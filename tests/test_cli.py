"""CLI 스모크 테스트 (subprocess): 종료 코드와 대화형 흐름 확인."""

from __future__ import annotations

import argparse
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


if __name__ == "__main__":
    unittest.main()
