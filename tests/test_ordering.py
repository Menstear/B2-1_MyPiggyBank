"""제한 조회의 메모리 사용과 전체 조회의 외부 정렬을 검증한다."""

from __future__ import annotations

import tempfile
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from budget_app import ordering
from budget_app.errors import StorageError, ValidationError
from budget_app.models import Transaction
from budget_app.ordering import newest_transactions


def transaction(number: int, date: str = "2024-01-01") -> Transaction:
    return Transaction(
        id=f"TX-{number:06d}", type="expense", date=date,
        amount=1000, category="food",
    )


class OrderingTest(unittest.TestCase):
    def test_date_then_numeric_id_descending(self) -> None:
        values = [
            transaction(999999),
            transaction(3, "2024-02-01"),
            transaction(1000000),
            transaction(4, "2023-12-31"),
        ]
        for limit in (None, 3):
            with self.subTest(limit=limit):
                got = list(newest_transactions(iter(values), limit))
                expected = ["TX-000003", "TX-1000000", "TX-999999", "TX-000004"]
                self.assertEqual([tx.id for tx in got], expected[:limit])

    def test_invalid_and_zero_limit(self) -> None:
        with self.assertRaises(ValidationError):
            newest_transactions([], -1)

        def must_not_read():
            self.fail("limit 0 must not consume the input")
            yield transaction(1)

        self.assertEqual(list(newest_transactions(must_not_read(), 0)), [])

    def test_limited_sort_retains_only_a_bounded_number_of_transactions(self) -> None:
        live: weakref.WeakValueDictionary[int, Transaction] = weakref.WeakValueDictionary()
        peak = 0

        def source():
            nonlocal peak
            for number in range(1, 10001):
                tx = transaction(number)
                live[number] = tx
                peak = max(peak, len(live))
                yield tx

        got = list(newest_transactions(source(), 3))
        self.assertEqual([tx.id for tx in got], ["TX-010000", "TX-009999", "TX-009998"])
        # 생성된 거래 객체를 모두 보관하는 구현이면 peak가 10000까지 늘어난다.
        self.assertLessEqual(peak, 8)

    def test_multiple_chunks_and_merge_levels_with_bounded_handles(self) -> None:
        values = [transaction(number, f"2024-01-{number % 28 + 1:02d}")
                  for number in range(100, 0, -1)]
        opened = []
        peak_handles = 0
        original_open = Path.open

        def tracking_open(path, *args, **kwargs):
            nonlocal peak_handles
            file = original_open(path, *args, **kwargs)
            opened.append(file)
            peak_handles = max(peak_handles, sum(not handle.closed for handle in opened))
            return file

        with patch.object(ordering, "_CHUNK_SIZE", 3), \
             patch.object(ordering, "_MERGE_FAN_IN", 2), \
             patch.object(Path, "open", tracking_open), \
             patch.object(ordering, "_combine_runs", wraps=ordering._combine_runs) as combine:
            got = list(newest_transactions(iter(values)))
        expected = sorted(values, key=lambda tx: (tx.date, int(tx.id[3:])), reverse=True)
        self.assertEqual(got, expected)
        self.assertGreater(combine.call_count, 2)
        self.assertTrue(all(len(call.args[0]) <= 2 for call in combine.call_args_list))
        self.assertLessEqual(peak_handles, 3)  # fan-in inputs + one output
        self.assertTrue(all(file.closed for file in opened))

    def test_full_sort_also_bounds_live_transaction_objects(self) -> None:
        live: weakref.WeakValueDictionary[int, Transaction] = weakref.WeakValueDictionary()
        peak = 0

        def source():
            nonlocal peak
            for number in range(1, 301):
                tx = transaction(number)
                live[number] = tx
                peak = max(peak, len(live))
                yield tx

        with patch.object(ordering, "_CHUNK_SIZE", 7), \
             patch.object(ordering, "_MERGE_FAN_IN", 2):
            ids = [tx.id for tx in newest_transactions(source())]
        self.assertEqual(ids, [f"TX-{number:06d}" for number in range(300, 0, -1)])
        self.assertLessEqual(peak, 8)

    def test_temporary_files_cleaned_after_completion_and_close(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            directories: list[Path] = []

            def temp_directory(**kwargs):
                directory = tempfile.TemporaryDirectory(dir=parent, **kwargs)
                directories.append(Path(directory.name))
                return directory

            with patch.object(ordering, "TemporaryDirectory", temp_directory), \
                 patch.object(ordering, "_CHUNK_SIZE", 2):
                self.assertEqual(len(list(newest_transactions(transaction(i) for i in range(5)))), 5)
                self.assertFalse(directories[0].exists())
                iterator = newest_transactions(transaction(i) for i in range(5))
                next(iterator)
                self.assertTrue(directories[1].exists())
                iterator.close()  # type: ignore[attr-defined]
                self.assertFalse(directories[1].exists())

    def test_temporary_files_cleaned_when_input_raises(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            def source():
                yield transaction(1)
                yield transaction(2)
                raise ValueError("bad source")

            original_directory = tempfile.TemporaryDirectory
            with patch.object(
                ordering, "TemporaryDirectory",
                side_effect=lambda **kwargs: original_directory(dir=parent, **kwargs),
            ), patch.object(ordering, "_CHUNK_SIZE", 1):
                with self.assertRaisesRegex(ValueError, "bad source"):
                    list(newest_transactions(source()))
            self.assertEqual(list(Path(parent).iterdir()), [])

    def test_temporary_io_errors_are_friendly_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            original_directory = tempfile.TemporaryDirectory
            with patch.object(
                ordering, "TemporaryDirectory",
                side_effect=lambda **kwargs: original_directory(dir=parent, **kwargs),
            ), patch.object(ordering, "_write_run", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(StorageError, "disk full"):
                    list(newest_transactions([transaction(1)]))
            self.assertEqual(list(Path(parent).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
