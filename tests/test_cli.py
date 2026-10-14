"""CLI 스모크 테스트 (subprocess): 종료 코드와 대화형 흐름 확인."""

from __future__ import annotations

import argparse
import csv
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from budget_app.cli import DEFAULT_DATA_DIR, build_parser, build_service, cmd_search
from budget_app.models import Transaction

ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str], data_dir: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "budget_app", *args, "--data-dir", data_dir],
        cwd=str(ROOT),
        input=stdin,
        capture_output=True,
        text=True,
    )


def run_in_workdir(args: list[str], workdir: Path) -> subprocess.CompletedProcess:
    """실제 기본 data 폴더도 임시 작업 폴더 안에 격리한다."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT), env.get("PYTHONPATH", "")) if part
    )
    return subprocess.run(
        [sys.executable, "-m", "budget_app", *args],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
    )


class CommonOptionsTest(unittest.TestCase):
    def test_defaults_and_explicit_overrides(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["category", "list"])
        self.assertEqual(args.data_dir, DEFAULT_DATA_DIR)
        self.assertFalse(args.verbose)
        args = parser.parse_args([
            "--data-dir", "first", "--verbose", "category", "--data-dir",
            "second", "list", "--data-dir", "third",
        ])
        self.assertEqual(args.data_dir, "third")
        self.assertTrue(args.verbose)

    def test_common_options_at_every_command_level(self) -> None:
        parser = build_parser()
        cases = [
            (["add"], []),
            (["list"], ["--limit", "1"]),
            (["search"], ["--q", "lunch"]),
            (["summary"], ["--month", "2024-01"]),
            (["budget", "set"], ["--month", "2024-01", "--amount", "1000"]),
            (["category", "add"], ["travel"]),
            (["category", "list"], []),
            (["category", "remove"], ["food", "--replace", "travel"]),
            (["update"], ["--id", "TX-000001", "--memo", "new"]),
            (["delete"], ["--id", "TX-000001"]),
            (["export"], ["--month", "2024-01", "--out", "out.csv"]),
            (["import"], ["--from", "input.csv"]),
        ]
        options = ["--data-dir", "selected", "--verbose"]
        for commands, arguments in cases:
            placements = [
                commands[:index] + options + commands[index:] + arguments
                for index in range(len(commands) + 1)
            ]
            placements.append(commands + arguments + options)
            for argv in placements:
                with self.subTest(argv=argv):
                    args = parser.parse_args(argv)
                    self.assertEqual(args.data_dir, "selected")
                    self.assertTrue(args.verbose)


class CliExitCodeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_import_prints_bounded_row_details_and_remaining_count(self) -> None:
        path = Path(self.data_dir) / "input.csv"
        path.write_text("date,type,category,amount\n"
                        + "2024-01-01,expense,food,-1\n" * 7
                        + "2024-01-02,expense,food,1000\n", encoding="utf-8")
        proc = run(["import", "--from", str(path)], self.data_dir)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("[완료] imported=1, skipped=7", proc.stdout)
        self.assertEqual(proc.stdout.count("[건너뜀]"), 5)
        self.assertIn("CSV 2행: 금액은 양수여야 합니다: -1", proc.stdout)
        self.assertIn("CSV 6행:", proc.stdout)
        self.assertIn("나머지 2건", proc.stdout)
        self.assertNotIn("CSV 7행:", proc.stdout)
        self.assertEqual(len(build_service(self.data_dir).list_transactions(10)), 1)

    def test_import_failure_prints_no_success_or_skip_report_and_preserves_data(self) -> None:
        service = build_service(self.data_dir)
        service.add_transaction(type="expense", date="2024-01-01", amount=1000, category="food")
        original = service.tx_repo.path.read_bytes()
        path = Path(self.data_dir) / "broken.csv"
        path.write_text("date,type,category,amount,memo\n"
                        "2024-01-01,expense,food,-1,잘못된 금액\n"
                        "2024-01-02,expense,food,2000,정상\n"
                        '2024-01-03,expense,food,3000,"닫히지 않은 따옴표\n', encoding="utf-8")
        proc = run(["import", "--from", str(path)], self.data_dir)
        self.assertEqual(proc.returncode, 6, proc.stdout + proc.stderr)
        self.assertIn("[오류]", proc.stdout)
        self.assertNotIn("[완료]", proc.stdout)
        self.assertNotIn("[건너뜀]", proc.stdout)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assertEqual(service.tx_repo.path.read_bytes(), original)

    def test_successful_import_has_no_skip_details(self) -> None:
        path = Path(self.data_dir) / "valid.csv"
        path.write_text("date,type,category,amount\n2024-01-01,expense,food,1000\n", encoding="utf-8")
        proc = run(["import", "--from", str(path)], self.data_dir)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "[완료] imported=1, skipped=0")

    def test_no_command_prints_help_nonzero(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "budget_app"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("usage", proc.stdout.lower())

    def test_interactive_add_success(self) -> None:
        stdin = "2024-01-15\nexpense\nfood\n15000\n점심\nmeal\n"
        proc = run(["add"], self.data_dir, stdin=stdin)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("id=TX-000001", proc.stdout)

    def test_delete_missing_exit_code(self) -> None:
        proc = run(["delete", "--id", "TX-999999"], self.data_dir)
        self.assertEqual(proc.returncode, 3)  # NotFoundError.exit_code
        self.assertIn("[오류]", proc.stdout)

    def test_export_without_condition_exit_code(self) -> None:
        proc = run(["export", "--out", f"{self.data_dir}/x.csv"], self.data_dir)
        self.assertEqual(proc.returncode, 2)  # ValidationError.exit_code

    def test_export_rejects_mixed_month_and_date_range_without_replacing_output(self) -> None:
        output = Path(self.data_dir) / "existing.csv"
        output.write_bytes(b"keep original output\n")
        for date_args in (["--from", "2024-02-01"], ["--to", "2024-01-31"],
                          ["--from", "2024-01-01", "--to", "2024-01-31"]):
            with self.subTest(date_args=date_args):
                proc = run(["export", "--month", "2024-01", *date_args,
                            "--out", str(output)], self.data_dir)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("함께 사용할 수 없습니다", proc.stdout)
                self.assertEqual(output.read_bytes(), b"keep original output\n")

    def test_export_february_includes_real_last_day_only(self) -> None:
        service = build_service(self.data_dir)
        dates = ("2023-01-31", "2023-02-01", "2023-02-28", "2023-03-01",
                 "2024-01-31", "2024-02-01", "2024-02-28", "2024-02-29", "2024-03-01")
        service.tx_repo.append_many(
            Transaction(f"TX-{index:06d}", "expense", date, 1000, "food")
            for index, date in enumerate(dates, start=1)
        )
        output = Path(self.data_dir) / "month.csv"
        for month, expected in (("2023-02", ["2023-02-01", "2023-02-28"]),
                                ("2024-02", ["2024-02-01", "2024-02-28", "2024-02-29"])):
            with self.subTest(month=month):
                proc = run(["export", "--month", month, "--out", str(output)], self.data_dir)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                with output.open(encoding="utf-8", newline="") as source:
                    self.assertEqual([row["date"] for row in csv.DictReader(source)], expected)

    def test_summary_rejects_negative_top_and_accepts_zero(self) -> None:
        build_service(self.data_dir).add_transaction(
            type="expense", date="2024-01-01", amount=1000, category="food",
        )
        invalid = run(["summary", "--month", "2024-01", "--top", "-1"], self.data_dir)
        self.assertEqual(invalid.returncode, 2, invalid.stdout + invalid.stderr)
        self.assertIn("음수", invalid.stdout)
        self.assertNotIn("총 지출", invalid.stdout)
        zero = run(["summary", "--month", "2024-01", "--top", "0"], self.data_dir)
        self.assertEqual(zero.returncode, 0, zero.stdout + zero.stderr)
        self.assertIn("총 지출: 1000원", zero.stdout)
        self.assertNotIn("TOP", zero.stdout)

    def test_search_accepts_normalized_dates_and_type(self) -> None:
        saved = build_service(self.data_dir).add_transaction(
            type="income", date="2024-01-01", amount=1000, category="food",
        )
        proc = run(["search", "--from", "2024-1-1", "--to", "2024-1-1",
                    "--type", " INCOME "], self.data_dir)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(saved.id, proc.stdout)

    def test_closed_pipe_during_search_exits_quietly_in_both_buffer_modes(self) -> None:
        service = build_service(self.data_dir)
        service.tx_repo.append_many(
            Transaction(f"TX-{index:06d}", "expense", "2024-01-01", 1000, "food",
                        memo="x" * 2000)
            for index in range(1, 251)
        )
        for flags in ([], ["-u"]):
            with self.subTest(flags=flags), subprocess.Popen(
                [sys.executable, *flags, "-m", "budget_app", "search",
                 "--data-dir", self.data_dir], cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ) as proc:
                try:
                    first_line = proc.stdout.readline()
                    self.assertTrue(first_line.startswith(b"TX-000250 |"), first_line)
                    proc.stdout.close()  # head -n 1처럼 첫 행 뒤 파이프 소비자가 종료한다.
                    self.assertEqual(proc.wait(timeout=10), 1)
                    self.assertEqual(proc.stderr.read(), b"")
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()

    def test_closed_pipe_on_final_flush_is_quiet_even_when_reporting_an_error(self) -> None:
        script = (
            "import sys; from budget_app.__main__ import main; "
            "sys.stdin.buffer.read(1); sys.exit(main(sys.argv[1:]))"
        )
        for command in (["search"], ["summary", "--month", "2024-01", "--top", "-1"]):
            with self.subTest(command=command), subprocess.Popen(
                [sys.executable, "-c", script, *command, "--data-dir", self.data_dir],
                cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ) as proc:
                try:
                    # 작은 출력이 print의 버퍼에만 남도록 실행 전에 소비자를 닫는다.
                    proc.stdout.close()
                    proc.stdin.write(b"x")
                    proc.stdin.close()
                    self.assertEqual(proc.wait(timeout=10), 1)
                    self.assertEqual(proc.stderr.read(), b"")
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()

    def test_summary_empty_month(self) -> None:
        proc = run(["summary", "--month", "2099-12"], self.data_dir)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("데이터 없음", proc.stdout)
        self.assertIn("총 수입: 0원", proc.stdout)
        self.assertIn("총 지출: 0원", proc.stdout)
        self.assertIn("잔액: 0원", proc.stdout)

    def test_summary_empty_month_displays_configured_budget(self) -> None:
        saved = run([
            "budget", "set", "--month", "2099-12", "--amount", "100000",
        ], self.data_dir)
        self.assertEqual(saved.returncode, 0, saved.stdout)
        proc = run(["summary", "--month", "2099-12"], self.data_dir)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("2099-12 데이터 없음", proc.stdout)
        self.assertIn("총 수입: 0원", proc.stdout)
        self.assertIn("총 지출: 0원", proc.stdout)
        self.assertIn("잔액: 0원", proc.stdout)
        self.assertIn("예산: 100000원 (사용률 0.0%)", proc.stdout)
        self.assertNotIn("[경고]", proc.stdout)

    def test_summary_huge_expense_prints_rate_warning_and_top(self) -> None:
        amount = 10 ** 400
        service = build_service(self.data_dir)
        service.add_transaction(
            type="expense", date="2024-01-01", amount=amount, category="food",
        )
        service.set_budget("2024-01", 1)

        proc = run(["summary", "--month", "2024-01", "--top", "1"], self.data_dir)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"총 지출: {amount}원", proc.stdout)
        self.assertIn(f"예산: 1원 (사용률 {amount}00.0%)", proc.stdout)
        self.assertIn(f"[경고] 예산을 {amount - 1}원 초과했습니다!", proc.stdout)
        self.assertIn(f"지출 TOP 1\n1) food {amount}원", proc.stdout)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assertNotIn("[오류]", proc.stdout)

    def test_summary_huge_budget_handles_tiny_and_normal_ratios(self) -> None:
        service = build_service(self.data_dir)
        tx = service.add_transaction(
            type="expense", date="2024-01-01", amount=1, category="food",
        )
        budget = 3 * 10 ** 400
        service.set_budget("2024-01", budget)
        for amount, rate in ((1, "0.0"), (10 ** 400, "33.3"), (budget, "100.0")):
            with self.subTest(amount=amount, rate=rate):
                service.update_transaction(tx.id, {"amount": amount})
                proc = run(["summary", "--month", "2024-01", "--top", "1"], self.data_dir)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn(f"예산: {budget}원 (사용률 {rate}%)", proc.stdout)
                self.assertIn(f"지출 TOP 1\n1) food {amount}원", proc.stdout)
                self.assertNotIn("[경고]", proc.stdout)
                self.assertNotIn("[오류]", proc.stdout)

    @unittest.skipUnless(hasattr(sys, "set_int_max_str_digits"), "Python 3.11+ 정수 자릿수 제한")
    def test_summary_rate_accepts_amount_at_integer_string_digit_limit(self) -> None:
        # 자릿수 제한은 이 subprocess에서만 낮춘다. 금액 입력과 JSON 저장도
        # 같은 제한 아래 실행하여 저장에 성공한 금액의 요약까지 검증한다.
        amount_text = "1" + "0" * 639
        script = """
import sys
from budget_app.cli import build_service
from budget_app.__main__ import main

sys.set_int_max_str_digits(640)
service = build_service(sys.argv[1])
service.add_transaction(
    type="expense", date="2024-01-01", amount=sys.argv[2], category="food",
)
service.set_budget("2024-01", 1)
sys.exit(main(["summary", "--month", "2024-01", "--top", "1", "--data-dir", sys.argv[1]]))
"""
        proc = subprocess.run(
            [sys.executable, "-c", script, self.data_dir, amount_text],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"예산: 1원 (사용률 {amount_text}00.0%)", proc.stdout)
        self.assertIn(f"[경고] 예산을 {'9' * 639}원 초과했습니다!", proc.stdout)
        self.assertIn(f"지출 TOP 1\n1) food {amount_text}원", proc.stdout)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    @unittest.skipUnless(hasattr(sys, "set_int_max_str_digits"), "Python 3.11+ 정수 자릿수 제한")
    def test_summary_imported_totals_exceed_integer_string_digit_limit(self) -> None:
        # 정상적으로 가져온 여러 금액의 합계가 제한을 넘는 경우를 검증한다.
        # 음수 잔액은 기본 4300자리, 양수 잔액은 격리한 640자리 제한을 사용한다.
        original_limit = sys.get_int_max_str_digits()
        script = """
import sys
from pathlib import Path
from budget_app.cli import build_service
from budget_app.__main__ import main

limit = int(sys.argv[3])
sys.set_int_max_str_digits(limit)
service = build_service(sys.argv[1])
assert service.import_csv(Path(sys.argv[2])) == (int(sys.argv[4]), 0)
service.set_budget("2024-01", 1)
result = main(["summary", "--month", "2024-01", "--top", "1", "--data-dir", sys.argv[1]])
assert sys.get_int_max_str_digits() == limit
try:
    int("9" * (limit + 1))
except ValueError:
    pass
else:
    raise AssertionError("The input digit limit must remain enabled")
sys.exit(result)
"""
        for digits, income_count in ((4300, 0), (640, 4)):
            with self.subTest(digits=digits, income_count=income_count):
                data_dir = Path(self.data_dir) / f"totals-{digits}"
                source = Path(self.data_dir) / f"totals-{digits}.csv"
                with source.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["date", "type", "category", "amount"])
                    for type_, count in (("expense", 2), ("income", income_count)):
                        for _ in range(count):
                            writer.writerow(["2024-01-01", type_, "food", "9" * digits])
                proc = subprocess.run(
                    [sys.executable, "-c", script, str(data_dir), str(source),
                     str(digits), str(2 + income_count)],
                    cwd=ROOT, capture_output=True, text=True,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                expense = "1" + "9" * (digits - 1) + "8"
                income = "3" + "9" * (digits - 1) + "6" if income_count else "0"
                balance = expense if income_count else "-" + expense
                over = "1" + "9" * (digits - 1) + "7"
                lines = proc.stdout.splitlines()
                for expected in (
                    f"총 수입: {income}원", f"총 지출: {expense}원", f"잔액: {balance}원",
                    f"예산: 1원 (사용률 {expense}00.0%)",
                    f"[경고] 예산을 {over}원 초과했습니다!", f"1) food {expense}원",
                ):
                    self.assertIn(expected, lines)
                self.assertNotIn("[오류]", proc.stdout)
                self.assertNotIn("Traceback", proc.stdout + proc.stderr)
                self.assertEqual(sys.get_int_max_str_digits(), original_limit)

    def test_summary_rate_rounds_one_decimal_with_exact_half_even_ties(self) -> None:
        service = build_service(self.data_dir)
        tx = service.add_transaction(
            type="expense", date="2024-01-01", amount=1, category="food",
        )
        cases = (
            (1, 3, "33.3"), (2, 3, "66.7"),
            (1, 2000, "0.0"), (3, 2000, "0.2"),
            (5, 2000, "0.2"), (7, 2000, "0.4"),
            (1, 2001, "0.0"), (1, 1999, "0.1"),
            (1999, 2000, "100.0"), (2001, 2000, "100.0"),
        )
        for expense, budget, expected in cases:
            with self.subTest(expense=expense, budget=budget):
                service.update_transaction(tx.id, {"amount": expense})
                service.set_budget("2024-01", budget)
                proc = run(["summary", "--month", "2024-01"], self.data_dir)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn(f"사용률 {expected}%", proc.stdout)
                # 반올림된 표시가 100.0%여도 경고는 실제 원 단위 금액으로 판단한다.
                self.assertEqual("[경고]" in proc.stdout, expense > budget)

    def test_root_data_dir_delete_preserves_default_ledger(self) -> None:
        workdir = Path(self.data_dir)
        selected = workdir / "selected"
        default = workdir / "data"
        for directory in (selected, default):
            build_service(str(directory)).add_transaction(
                type="expense", date="2024-01-01", amount=1000,
                category="food", memo=directory.name,
            )
        original_default = (default / "transactions.jsonl").read_bytes()
        proc = run_in_workdir([
            "--data-dir", str(selected), "--verbose", "delete", "--id", "TX-000001",
        ], workdir)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual((default / "transactions.jsonl").read_bytes(), original_default)
        self.assertEqual(list(build_service(str(selected)).tx_repo.stream()), [])
        self.assertIn("실행: cmd_delete", proc.stderr)
        self.assertIn("실행 시간", proc.stderr)

    def test_nested_options_select_storage_and_keep_verbose_at_each_position(self) -> None:
        workdir = Path(self.data_dir)
        commands = ["budget", "set"]
        arguments = ["--month", "2024-01", "--amount", "1000"]
        for index in range(4):
            selected = workdir / f"selected-{index}"
            options = ["--data-dir", str(selected), "--verbose"]
            if index < 3:
                argv = commands[:index] + options + commands[index:] + arguments
            else:
                argv = commands + arguments + options
            with self.subTest(argv=argv):
                proc = run_in_workdir(argv, workdir)
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assertEqual(build_service(str(selected)).budgets.get("2024-01").amount, 1000)
                self.assertFalse((workdir / "data").exists())
                self.assertIn("실행: cmd_budget", proc.stderr)

    def test_list_and_search_share_date_order_and_tie_breaker(self) -> None:
        service = build_service(self.data_dir)
        transactions = [
            service.add_transaction(
                type="expense", date=date, amount=1000, category="food",
            )
            for date in ("2024-02-01", "2024-02-01", "2024-01-01")
        ]
        expected = [transactions[1].id, transactions[0].id, transactions[2].id]
        for command in ("list", "search"):
            with self.subTest(command=command):
                proc = run([command], self.data_dir)
                self.assertEqual(proc.returncode, 0, proc.stdout)
                actual = [line.split(" | ")[0] for line in proc.stdout.splitlines()]
                self.assertEqual(actual, expected)

    def test_category_remove_in_use_exit_code(self) -> None:
        run(["add"], self.data_dir, stdin="2024-01-15\nexpense\nfood\n1000\n\n\n")
        proc = run(["category", "remove", "food"], self.data_dir)
        self.assertEqual(proc.returncode, 4)  # CategoryError.exit_code


class SearchIteratorOutputTest(unittest.TestCase):
    def search_output(self, transactions: list[Transaction]) -> str:
        # 비어 있는 generator도 truthy이므로 실제 순회로 빈 결과를 판별해야 한다.
        service = Mock()
        service.search.return_value = (tx for tx in transactions)
        args = argparse.Namespace(
            data_dir="unused", date_from=None, date_to=None, category=None,
            type=None, q=None, tag=None, limit=None,
        )
        output = io.StringIO()
        with patch("budget_app.cli.build_service", return_value=service):
            with redirect_stdout(output):
                self.assertEqual(cmd_search(args), 0)
        service.search.assert_called_once()
        return output.getvalue()

    def test_empty_iterator_displays_no_results(self) -> None:
        self.assertEqual(self.search_output([]), "검색 결과가 없습니다.\n")

    def test_nonempty_iterator_prints_each_transaction_once(self) -> None:
        transactions = [
            Transaction("TX-000002", "expense", "2024-02-01", 1000, "food"),
            Transaction("TX-000001", "expense", "2024-01-01", 2000, "food"),
        ]
        output = self.search_output(transactions)
        self.assertEqual(len(output.splitlines()), 2)
        self.assertTrue(output.startswith("TX-000002 |"))
        self.assertEqual(output.count("TX-000001 |"), 1)
        self.assertNotIn("검색 결과가 없습니다.", output)

    def test_cancelled_output_closes_search_generator(self) -> None:
        closed = False

        def results():
            nonlocal closed
            try:
                yield Transaction("TX-000001", "expense", "2024-01-01", 1000, "food")
                yield Transaction("TX-000002", "expense", "2024-01-02", 2000, "food")
            finally:
                closed = True

        service = Mock()
        service.search.return_value = results()
        args = argparse.Namespace(
            data_dir="unused", date_from=None, date_to=None, category=None,
            type=None, q=None, tag=None, limit=None,
        )
        with patch("budget_app.cli.build_service", return_value=service), \
             patch("budget_app.cli.print", side_effect=KeyboardInterrupt), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                cmd_search(args)
        self.assertEqual(stopped.exception.code, 130)
        self.assertTrue(closed)

    def test_broken_pipe_closes_search_generator_and_reaches_entrypoint(self) -> None:
        closed = False

        def results():
            nonlocal closed
            try:
                yield Transaction("TX-000001", "expense", "2024-01-01", 1000, "food")
            finally:
                closed = True

        service = Mock()
        service.search.return_value = results()
        args = argparse.Namespace(
            data_dir="unused", date_from=None, date_to=None, category=None,
            type=None, q=None, tag=None, limit=None,
        )
        with patch("budget_app.cli.build_service", return_value=service), \
             patch("budget_app.cli.print", side_effect=BrokenPipeError), \
             patch("builtins.print") as error_output:
            with self.assertRaises(BrokenPipeError):
                cmd_search(args)
        self.assertTrue(closed)
        error_output.assert_not_called()


if __name__ == "__main__":
    unittest.main()
