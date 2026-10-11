"""서비스 계층 테스트 (service): CRUD / 검색 / 요약 / 예산 / 카테고리 / CSV."""

from __future__ import annotations

import csv
import os
import tempfile
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from budget_app.errors import CategoryError, ImportExportError, NotFoundError, ValidationError
from budget_app.models import Transaction
from budget_app.repository import (
    BudgetStore,
    CategoryStore,
    TransactionRepository,
    init_storage,
)
from budget_app.service import BudgetService, SearchQuery


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        tx = TransactionRepository(self.base)
        cat = CategoryStore(self.base)
        bud = BudgetStore(self.base)
        init_storage(self.base, tx, cat, bud)  # 기본 카테고리 생성
        self.svc = BudgetService(tx, cat, bud)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add(self, **over: object):
        params = dict(
            type="expense", date="2024-01-15", amount=1000, category="food"
        )
        params.update(over)
        return self.svc.add_transaction(**params)  # type: ignore[arg-type]


class AddTest(ServiceTestBase):
    def test_add_returns_id_and_persists(self) -> None:
        tx = self.add(amount=15000, memo="점심", tags=["meal"])
        self.assertEqual(tx.id, "TX-000001")
        self.assertEqual(len(self.svc.list_transactions(10)), 1)

    def test_add_rejects_unknown_category(self) -> None:
        with self.assertRaises(CategoryError):
            self.add(category="unknown")

    def test_add_rejects_bad_amount(self) -> None:
        with self.assertRaises(ValidationError):
            self.add(amount=-5)


class ListTest(ServiceTestBase):
    def test_newest_first_and_limit(self) -> None:
        self.add(date="2024-01-01")  # TX-000001
        self.add(date="2024-01-02")  # TX-000002
        self.add(date="2024-01-03")  # TX-000003
        got = self.svc.list_transactions(2)
        # 거래 날짜 최신순, limit 2
        self.assertEqual([t.id for t in got], ["TX-000003", "TX-000002"])

    def test_old_transaction_added_last_does_not_hide_latest_date(self) -> None:
        newest = self.add(date="2024-12-31")
        self.add(date="2024-01-01")
        self.assertEqual(self.svc.list_transactions(1), [newest])

    def test_date_update_changes_list_and_search_order(self) -> None:
        first = self.add(date="2024-12-31")
        second = self.add(date="2024-01-01")
        self.svc.update_transaction(second.id, {"date": "2025-01-01"})
        listed = self.svc.list_transactions(2)
        self.assertEqual([tx.id for tx in listed], [second.id, first.id])
        self.assertEqual(list(self.svc.search(SearchQuery(), limit=2)), listed)


class SearchTest(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.svc.add_category("coffee")
        self.add(date="2024-01-10", amount=1000, category="food", memo="점심", tags=["meal"])
        self.add(date="2024-02-10", amount=2000, category="coffee", memo="아메리카노", tags=["cafe"])
        self.add(date="2024-03-10", type="income", amount=3000, category="salary")

    def test_filter_by_type(self) -> None:
        res = self.svc.search(SearchQuery(type="income"))
        self.assertEqual([t.category for t in res], ["salary"])

    def test_filter_by_date_range(self) -> None:
        res = self.svc.search(SearchQuery(date_from="2024-02-01", date_to="2024-02-28"))
        self.assertEqual([t.category for t in res], ["coffee"])

    def test_filter_by_keyword_and_tag(self) -> None:
        self.assertEqual(len(list(self.svc.search(SearchQuery(q="아메리카노")))), 1)
        self.assertEqual(len(list(self.svc.search(SearchQuery(tag="meal")))), 1)

    def test_results_newest_date_first(self) -> None:
        res = self.svc.search(SearchQuery())
        self.assertEqual([t.date for t in res], ["2024-03-10", "2024-02-10", "2024-01-10"])

    def test_limited_search_is_lazy_and_does_not_retain_all_matches(self) -> None:
        live: weakref.WeakValueDictionary[int, Transaction] = weakref.WeakValueDictionary()
        seen = 0
        peak = 0

        def source():
            nonlocal seen, peak
            for number in range(1, 2001):
                tx = Transaction(
                    id=f"TX-{number:06d}", type="expense", date="2024-01-01",
                    amount=1000, category="food", memo="match" if number % 2 else "other",
                )
                live[number] = tx
                seen += 1
                peak = max(peak, len(live))
                yield tx

        with patch.object(self.svc.tx_repo, "stream", side_effect=source):
            result = self.svc.search(SearchQuery(q="match"), limit=2)
            self.assertEqual(seen, 0)
            got = list(result)
        self.assertEqual([tx.id for tx in got], ["TX-001999", "TX-001997"])
        self.assertEqual(seen, 2000)
        self.assertLessEqual(peak, 7)

    def test_negative_limits_are_validation_errors(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.list_transactions(-1)
        with self.assertRaises(ValidationError):
            list(self.svc.search(SearchQuery(), limit=-1))


class SummaryTest(ServiceTestBase):
    def test_totals_balance_and_topn(self) -> None:
        self.add(date="2024-01-05", type="income", amount=3000000, category="salary")
        self.add(date="2024-01-06", amount=150000, category="rent")
        self.add(date="2024-01-07", amount=15000, category="food")
        self.add(date="2024-01-08", amount=20000, category="transport")
        s = self.svc.summary("2024-01")
        self.assertEqual(s["total_income"], 3000000)
        self.assertEqual(s["total_expense"], 185000)
        self.assertEqual(s["balance"], 2815000)
        top = s["expense_by_category"].most_common(2)
        self.assertEqual(top, [("rent", 150000), ("transport", 20000)])

    def test_empty_month(self) -> None:
        s = self.svc.summary("2099-12")
        self.assertEqual(s["count"], 0)

    def test_non_canonical_month_matches(self) -> None:
        # 회귀 방지: '2024-1' 과 '2024-1-5' 저장값이 정규화돼 매칭돼야 한다.
        self.add(date="2024-1-5", amount=5000, category="food")
        s = self.svc.summary("2024-1")
        self.assertEqual(s["count"], 1)
        self.assertEqual(s["total_expense"], 5000)

    def test_budget_attached(self) -> None:
        self.svc.set_budget("2024-01", 500000)
        self.add(date="2024-01-05", amount=1000, category="food")
        s = self.svc.summary("2024-01")
        assert s["budget"] is not None
        self.assertEqual(s["budget"].amount, 500000)


class CategoryTest(ServiceTestBase):
    def test_remove_blocks_when_in_use(self) -> None:
        self.add(category="food")
        with self.assertRaises(CategoryError):
            self.svc.remove_category("food")

    def test_remove_with_replacement_reassigns(self) -> None:
        self.add(category="food")
        self.add(category="food")
        moved = self.svc.remove_category("food", replacement="etc")
        self.assertEqual(moved, 2)
        self.assertFalse(self.svc.categories.exists("food"))
        cats = {t.category for t in self.svc.list_transactions(10)}
        self.assertEqual(cats, {"etc"})

    def test_remove_missing_raises(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.remove_category("nope")

    def test_add_duplicate_raises(self) -> None:
        with self.assertRaises(CategoryError):
            self.svc.add_category("food")  # 기본 카테고리와 중복


class UpdateDeleteTest(ServiceTestBase):
    def test_update_only_given_fields(self) -> None:
        tx = self.add(amount=1000, memo="old")
        self.svc.update_transaction(tx.id, {"amount": 2000})
        reloaded = self.svc.tx_repo.get(tx.id)
        assert reloaded is not None
        self.assertEqual(reloaded.amount, 2000)
        self.assertEqual(reloaded.memo, "old")  # 미지정 필드 보존

    def test_update_no_fields_raises(self) -> None:
        tx = self.add()
        with self.assertRaises(ValidationError):
            self.svc.update_transaction(tx.id, {})

    def test_update_unknown_category_raises(self) -> None:
        tx = self.add()
        with self.assertRaises(CategoryError):
            self.svc.update_transaction(tx.id, {"category": "nope"})

    def test_delete_missing_raises(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.delete_transaction("TX-999999")


class CsvTest(ServiceTestBase):
    def test_export_rejects_all_internal_storage_paths_without_changes(self) -> None:
        self.add()
        self.svc.set_budget("2024-01", 1000)
        paths = [self.svc.tx_repo.path, self.svc.categories.path, self.svc.budgets.path]
        before = {path: path.read_bytes() for path in paths}
        for path in paths:
            with self.subTest(path=path.name):
                with self.assertRaises(ImportExportError):
                    self.svc.export_csv(path, SearchQuery())
                self.assertEqual({p: p.read_bytes() for p in paths}, before)
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_export_rejects_storage_aliases(self) -> None:
        self.add()
        original = self.svc.tx_repo.path.read_bytes()
        aliases = [self.base / "symlink.csv", self.base / "hardlink.csv"]
        aliases[0].symlink_to(self.svc.tx_repo.path)
        os.link(self.svc.tx_repo.path, aliases[1])
        for alias in aliases:
            with self.subTest(alias=alias.name):
                with self.assertRaises(ImportExportError):
                    self.svc.export_csv(alias, SearchQuery())
                self.assertEqual(self.svc.tx_repo.path.read_bytes(), original)
                self.assertEqual(alias.read_bytes(), original)

    def test_export_invalid_path_is_friendly_and_preserves_storage(self) -> None:
        self.add()
        original = self.svc.tx_repo.path.read_bytes()
        with patch.object(Path, "resolve", side_effect=RuntimeError("symlink loop")):
            with self.assertRaisesRegex(ImportExportError, "출력 경로"):
                self.svc.export_csv(self.base / "loop.csv", SearchQuery())
        self.assertEqual(self.svc.tx_repo.path.read_bytes(), original)
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_export_failure_preserves_existing_csv_and_storage(self) -> None:
        self.add()
        original = self.svc.tx_repo.path.read_bytes()
        out = self.base / "out.csv"
        out.write_text("previous export", encoding="utf-8")
        with patch("budget_app.service.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(ImportExportError):
                self.svc.export_csv(out, SearchQuery())
        self.assertEqual(out.read_text(encoding="utf-8"), "previous export")
        self.assertEqual(self.svc.tx_repo.path.read_bytes(), original)
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_csv_roundtrip_accepts_long_and_quoted_memo(self) -> None:
        memo = "  " + "한" * 131073 + ',"줄\n바꿈"  '
        self.add(date="2024-01-01", memo="short")
        self.add(date="2024-01-02", memo=memo, tags=["x", "y"])
        out = self.base / "out.csv"
        self.assertEqual(self.svc.export_csv(out, SearchQuery()), 2)

        target = self.base / "new-data"
        tx = TransactionRepository(target)
        cats = CategoryStore(target)
        budgets = BudgetStore(target)
        init_storage(target, tx, cats, budgets)
        destination = BudgetService(tx, cats, budgets)
        previous_limit = csv.field_size_limit()
        self.assertEqual(destination.import_csv(out), (2, 0))
        self.assertEqual(csv.field_size_limit(), previous_limit)
        imported = destination.list_transactions(2)
        self.assertEqual(imported[0].memo, memo)
        self.assertEqual(imported[0].tags, ["x", "y"])
        self.assertEqual(imported[1].memo, "short")

    def test_late_csv_parse_failure_and_retry_preserve_original_transactions(self) -> None:
        self.add(memo="existing")
        original = self.svc.tx_repo.path.read_bytes()
        path = self.base / "broken.csv"
        path.write_text(
            "date,type,category,amount,memo,tags\n"
            "2024-01-01,expense,food,1000,valid,\n"
            '2024-01-02,expense,food,2000,"unterminated\n',
            encoding="utf-8",
        )
        previous_limit = csv.field_size_limit()
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                with self.assertRaises(ImportExportError):
                    self.svc.import_csv(path)
                self.assertEqual(self.svc.tx_repo.path.read_bytes(), original)
                self.assertEqual(csv.field_size_limit(), previous_limit)
                self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_late_invalid_utf8_does_not_partially_import(self) -> None:
        self.add()
        original = self.svc.tx_repo.path.read_bytes()
        path = self.base / "invalid-encoding.csv"
        path.write_bytes(
            b"date,type,category,amount,memo,tags\n"
            + b"2024-01-01,expense,food,1000,valid,\n" * 1000
            + b"2024-01-02,expense,food,1000,\xff,\n"
        )
        with self.assertRaises(ImportExportError):
            self.svc.import_csv(path)
        self.assertEqual(self.svc.tx_repo.path.read_bytes(), original)
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_import_commit_failure_preserves_original_and_can_retry(self) -> None:
        self.add()
        original = self.svc.tx_repo.path.read_bytes()
        path = self.base / "valid.csv"
        path.write_text(
            "date,type,category,amount\n2024-02-01,expense,food,8000\n",
            encoding="utf-8",
        )
        from budget_app.errors import StorageError

        with patch("budget_app.repository.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(StorageError):
                self.svc.import_csv(path)
        self.assertEqual(self.svc.tx_repo.path.read_bytes(), original)
        self.assertEqual(list(self.base.glob("*.tmp")), [])
        self.assertEqual(self.svc.import_csv(path), (1, 0))
        self.assertEqual([tx.id for tx in self.svc.list_transactions(10)], ["TX-000002", "TX-000001"])

    def test_import_of_old_dates_keeps_list_and_search_latest_first(self) -> None:
        newest = self.add(date="2024-12-31")
        path = self.base / "old.csv"
        path.write_text(
            "date,type,category,amount\n2024-01-01,expense,food,1000\n",
            encoding="utf-8",
        )
        self.assertEqual(self.svc.import_csv(path), (1, 0))
        self.assertEqual(self.svc.list_transactions(1), [newest])
        self.assertEqual(list(self.svc.search(SearchQuery(), limit=1)), [newest])

    def test_import_empty_or_all_skipped_leaves_storage_unchanged(self) -> None:
        self.add()
        original = self.svc.tx_repo.path.read_bytes()
        for body, expected in (("", (0, 0)), ("2024-01-01,expense,food,-1\n", (0, 1))):
            path = self.base / "nothing.csv"
            path.write_text("date,type,category,amount\n" + body, encoding="utf-8")
            self.assertEqual(self.svc.import_csv(path), expected)
            self.assertEqual(self.svc.tx_repo.path.read_bytes(), original)

    def test_export_then_import_roundtrip(self) -> None:
        self.add(date="2024-01-05", amount=1000, category="food", memo="a", tags=["x", "y"])
        self.add(date="2024-01-06", type="income", amount=3000, category="salary")
        out = self.base / "out.csv"
        n = self.svc.export_csv(out, SearchQuery(date_from="2024-01-01", date_to="2024-01-31"))
        self.assertEqual(n, 2)

        # 새 서비스(빈 거래)로 가져오기
        base2 = self.base / "data2"
        tx2 = TransactionRepository(base2)
        cat2 = CategoryStore(base2)
        bud2 = BudgetStore(base2)
        init_storage(base2, tx2, cat2, bud2)
        svc2 = BudgetService(tx2, cat2, bud2)
        imported, skipped = svc2.import_csv(out)
        self.assertEqual((imported, skipped), (2, 0))
        got = svc2.list_transactions(10)
        self.assertEqual(len(got), 2)
        # 태그 보존 확인
        tagged = [t for t in got if t.tags]
        self.assertEqual(tagged[0].tags, ["x", "y"])

    def test_import_skips_bad_and_unknown_category(self) -> None:
        csv_path = self.base / "in.csv"
        csv_path.write_text(
            "date,type,category,amount,memo,tags\n"
            "2024-02-01,expense,food,8000,ok,\n"       # 정상
            "2024-13-99,expense,food,5000,baddate,\n"  # 날짜 오류 -> skip
            "2024-02-02,expense,unknown,1000,nocat,\n" # 미등록 카테고리 -> skip
            "2024-02-03,expense,food,-1,badamount,\n", # 음수 금액 -> skip
            encoding="utf-8",
        )
        imported, skipped = self.svc.import_csv(csv_path)
        self.assertEqual((imported, skipped), (1, 3))

    def test_import_report_identifies_reasons_and_multiline_record_end(self) -> None:
        path = self.base / "report.csv"
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["date", "type", "category", "amount", "memo"])
            writer.writerow(["2024-02-01", "expense", "food", "1000", "정상"])
            writer.writerow(["2024-13-99", "expense", "food", "1000", "두\n줄"])
            writer.writerow(["2024-02-02", "expense", "unknown", "1000", "미등록"])
            writer.writerow(["2024-02-03", "expense", "food", "-1", "금액 오류"])
            writer.writerow(["2024-02-04", "expense", "food", "2000", "정상"])
        report = self.svc.import_csv_report(path)
        self.assertEqual((report.imported, report.skipped), (2, 3))
        self.assertEqual([issue.line for issue in report.samples], [4, 5, 6])
        self.assertIn("날짜", report.samples[0].message)
        self.assertIn("unknown", report.samples[1].message)
        self.assertIn("금액", report.samples[2].message)
        self.assertTrue(all(issue.hint for issue in report.samples))
        self.assertEqual([tx.id for tx in self.svc.list_transactions(10)], ["TX-000002", "TX-000001"])

    def test_import_report_bounds_samples_and_preserves_all_skipped_storage(self) -> None:
        self.add()
        original = self.svc.tx_repo.path.read_bytes()
        path = self.base / "skipped.csv"
        path.write_text("date,type,category,amount\n" + "2024-01-01,expense,food,-1\n" * 1000,
                        encoding="utf-8")
        report = self.svc.import_csv_report(path)
        self.assertEqual((report.imported, report.skipped), (0, 1000))
        self.assertEqual([issue.line for issue in report.samples], [2, 3, 4, 5, 6])
        self.assertEqual(self.svc.tx_repo.path.read_bytes(), original)

    def test_import_loads_categories_once_and_refreshes_on_next_call(self) -> None:
        path = self.base / "categories.csv"
        path.write_text("date,type,category,amount\n" + "2024-01-01,expense,travel,1000\n" * 10,
                        encoding="utf-8")
        with patch.object(self.svc.categories, "list_names", wraps=self.svc.categories.list_names) as names, \
             patch.object(self.svc.categories, "exists", side_effect=AssertionError("행마다 재조회")):
            self.assertEqual(self.svc.import_csv(path), (0, 10))
            names.assert_called_once_with()
        self.svc.add_category("travel")
        with patch.object(self.svc.categories, "list_names", wraps=self.svc.categories.list_names) as names:
            report = self.svc.import_csv_report(path)
            self.assertEqual((report.imported, report.skipped, report.samples), (10, 0, ()))
            names.assert_called_once_with()

    def test_import_rejects_bad_header(self) -> None:
        csv_path = self.base / "bad.csv"
        csv_path.write_text("foo,bar\n1,2\n", encoding="utf-8")
        from budget_app.errors import ImportExportError

        with self.assertRaises(ImportExportError):
            self.svc.import_csv(csv_path)

    def test_import_missing_file_raises(self) -> None:
        from budget_app.errors import ImportExportError

        with self.assertRaises(ImportExportError):
            self.svc.import_csv(self.base / "does_not_exist.csv")


if __name__ == "__main__":
    unittest.main()
