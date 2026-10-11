"""저장소 계층 테스트 (repository): 스트리밍 / 원자적 수정·삭제 / 저장소별 동작."""

from __future__ import annotations

import csv
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO
from unittest.mock import patch

from budget_app.errors import NotFoundError, StorageError
from budget_app.models import Transaction
from budget_app.repository import (
    DEFAULT_CATEGORIES,
    BudgetStore,
    CategoryStore,
    TransactionRepository,
    init_storage,
)


def make_tx(n: int, **over: object) -> Transaction:
    base = dict(
        id=f"TX-{n:06d}",
        type="expense",
        date="2024-01-15",
        amount=1000,
        category="food",
        memo="",
        tags=[],
    )
    base.update(over)
    return Transaction.from_dict(base)  # type: ignore[arg-type]


class AppendFileProbe:
    """실제 파일에 일부 바이트를 쓴 후 지정한 I/O 경계에서 실패한다."""

    def __init__(self, file: BinaryIO, stage: str, error: BaseException) -> None:
        self.file = file
        self.stage = stage
        self.error = error

    def __getattr__(self, name: str) -> Any:
        return getattr(self.file, name)

    def __enter__(self) -> "AppendFileProbe":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.file.close()
        if self.stage == "close" and exc_type is None:
            raise self.error

    def write(self, data: memoryview) -> int:
        if self.stage == "write":
            self.file.write(data[:7])
            raise self.error
        if self.stage == "short_write":
            return self.file.write(data[:7])
        return self.file.write(data)

    def flush(self) -> None:
        self.file.flush()
        if self.stage == "flush":
            raise self.error


class TransactionRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.repo = TransactionRepository(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_and_stream_roundtrip(self) -> None:
        self.repo.append(make_tx(1, memo="첫번째", tags=["a", "b"]))
        self.repo.append(make_tx(2))
        txs = list(self.repo.stream())
        self.assertEqual([t.id for t in txs], ["TX-000001", "TX-000002"])
        self.assertEqual(txs[0].tags, ["a", "b"])

    def test_stream_missing_file_is_empty(self) -> None:
        self.assertEqual(list(self.repo.stream()), [])

    def _open_probe(
        self, stage: str, error: BaseException, rollback_error: OSError | None = None
    ) -> AbstractContextManager[Any]:
        real_open = Path.open

        def open_with_probe(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == self.repo.path and args and args[0] == "r+b" and rollback_error is not None:
                raise rollback_error
            file = real_open(path, *args, **kwargs)
            if path == self.repo.path and args and args[0] == "ab":
                return AppendFileProbe(file, stage, error)
            return file

        return patch.object(Path, "open", new=open_with_probe)

    def test_append_io_failures_restore_original_bytes(self) -> None:
        self.repo.append(make_tx(1, memo="원본 거래"))
        original = self.repo.path.read_bytes()
        for stage in ("write", "flush", "close", "fsync"):
            with self.subTest(stage=stage):
                with self._open_probe(stage, OSError("디스크 부족")):
                    if stage == "fsync":
                        with patch("budget_app.repository.os.fsync", side_effect=[OSError("동기화 실패"), None]):
                            with self.assertRaises(StorageError):
                                self.repo.append(make_tx(2, memo="추가 거래"))
                    else:
                        with self.assertRaises(StorageError):
                            self.repo.append(make_tx(2, memo="추가 거래"))
                self.assertEqual(self.repo.path.read_bytes(), original)
                self.assertEqual([t.id for t in self.repo.stream()], ["TX-000001"])
                self.assertEqual(list(self.base.glob("*.tmp")), [])
        self.repo.append(make_tx(2))
        self.assertEqual([t.id for t in self.repo.stream()], ["TX-000001", "TX-000002"])

    def test_interrupted_append_restores_original_bytes(self) -> None:
        self.repo.append(make_tx(1))
        original = self.repo.path.read_bytes()
        with self._open_probe("write", KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.repo.append(make_tx(2))
        self.assertEqual(self.repo.path.read_bytes(), original)
        self.assertEqual([t.id for t in self.repo.stream()], ["TX-000001"])

    def test_failed_append_does_not_leave_new_file(self) -> None:
        with self._open_probe("write", OSError("디스크 부족")):
            with self.assertRaises(StorageError):
                self.repo.append(make_tx(1))
        self.assertFalse(self.repo.path.exists())
        self.assertEqual(list(self.repo.stream()), [])

    def test_append_reports_rollback_failure_without_masking_cause(self) -> None:
        for error in (OSError("쓰기 실패"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.repo.path.write_bytes(b"")
                self.repo.append(make_tx(1))
                with self._open_probe("write", error, OSError("복구 접근 실패")):
                    with self.assertRaises(StorageError) as caught:
                        self.repo.append(make_tx(2))
                self.assertIn("복구", str(caught.exception))
                self.assertIn("복구 접근 실패", str(caught.exception))
                self.assertIs(caught.exception.__cause__, error)

    def test_append_retries_short_writes(self) -> None:
        with self._open_probe("short_write", OSError()):
            self.repo.append(make_tx(1, memo="여러 번 나누어 쓰기"))
        self.assertEqual(list(self.repo.stream()), [make_tx(1, memo="여러 번 나누어 쓰기")])

    def test_append_many_keeps_existing_and_adds_stream(self) -> None:
        self.repo.append(make_tx(1))
        original = self.repo.path.read_bytes()

        def additions() -> Iterator[Transaction]:
            for n in range(2, 5):
                self.assertEqual(self.repo.path.read_bytes(), original)
                yield make_tx(n)

        self.repo.append_many(additions())
        self.assertEqual([t.id for t in self.repo.stream()], [f"TX-{n:06d}" for n in range(1, 5)])
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_append_many_empty_does_not_change_file(self) -> None:
        self.repo.append_many(iter(()))
        self.assertFalse(self.repo.path.exists())
        self.repo.append(make_tx(1))
        with self.repo.path.open("ab") as file:
            file.write(b"\n  \n")
        original = self.repo.path.read_bytes()
        self.repo.append_many(iter(()))
        self.assertEqual(self.repo.path.read_bytes(), original)

    def test_append_many_late_generator_errors_preserve_original(self) -> None:
        self.repo.append(make_tx(1))
        original = self.repo.path.read_bytes()
        errors = (
            csv.Error("CSV 오류"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "잘못된 UTF-8"),
            ValueError("생성 오류"),
            KeyboardInterrupt(),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                def additions() -> Iterator[Transaction]:
                    yield make_tx(2)
                    yield make_tx(3)
                    raise error

                with self.assertRaises(type(error)):
                    self.repo.append_many(additions())
                self.assertEqual(self.repo.path.read_bytes(), original)
                self.assertEqual([t.id for t in self.repo.stream()], ["TX-000001"])
                self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_next_id_sequence(self) -> None:
        self.assertEqual(self.repo.next_id(), "TX-000001")
        self.repo.append(make_tx(1))
        self.assertEqual(self.repo.next_id(), "TX-000002")

    def test_update_applies_and_persists(self) -> None:
        self.repo.append(make_tx(1))
        self.repo.append(make_tx(2))
        updated = self.repo.update("TX-000001", {"amount": 9999, "memo": "수정"})
        self.assertEqual(updated.amount, 9999)
        reloaded = self.repo.get("TX-000001")
        assert reloaded is not None
        self.assertEqual(reloaded.amount, 9999)
        self.assertEqual(reloaded.memo, "수정")
        # 다른 레코드는 보존
        other = self.repo.get("TX-000002")
        assert other is not None
        self.assertEqual(other.amount, 1000)

    def test_update_missing_raises(self) -> None:
        with self.assertRaises(NotFoundError):
            self.repo.update("TX-999999", {"amount": 1})

    def test_delete(self) -> None:
        self.repo.append(make_tx(1))
        self.repo.append(make_tx(2))
        self.assertTrue(self.repo.delete("TX-000001"))
        self.assertFalse(self.repo.delete("TX-000001"))  # 이미 없음
        self.assertEqual([t.id for t in self.repo.stream()], ["TX-000002"])

    def test_update_and_delete_replace_failures_preserve_original(self) -> None:
        self.repo.append(make_tx(1))
        self.repo.append(make_tx(2))
        original = self.repo.path.read_bytes()
        operations = (
            lambda: self.repo.update("TX-000001", {"memo": "수정"}),
            lambda: self.repo.delete("TX-000001"),
        )
        for operation in operations:
            with patch("budget_app.repository.os.replace", side_effect=OSError("교체 실패")):
                with self.assertRaises(StorageError):
                    operation()
            self.assertEqual(self.repo.path.read_bytes(), original)
            self.assertEqual([t.id for t in self.repo.stream()], ["TX-000001", "TX-000002"])
            self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_rewrite_generator_failure_cleans_temporary_file(self) -> None:
        self.repo.append(make_tx(1))
        original = self.repo.path.read_bytes()

        def transactions() -> Iterator[Transaction]:
            yield make_tx(2)
            raise RuntimeError("레코드 생성 실패")

        with self.assertRaises(RuntimeError):
            self.repo.rewrite(transactions())
        self.assertEqual(self.repo.path.read_bytes(), original)
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_rewrite_open_failure_closes_descriptor_and_cleans_temporary_file(self) -> None:
        self.repo.append(make_tx(1))
        original = self.repo.path.read_bytes()
        with patch("budget_app.repository.os.fdopen", side_effect=OSError("열기 실패")):
            with patch("budget_app.repository.os.close", wraps=os.close) as close:
                with self.assertRaises(StorageError):
                    self.repo.rewrite([make_tx(2)])
                close.assert_called_once()
        self.assertEqual(self.repo.path.read_bytes(), original)
        self.assertEqual(list(self.base.glob("*.tmp")), [])


class CategoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CategoryStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ensure_initialized_creates_defaults(self) -> None:
        self.store.ensure_initialized()
        self.assertEqual(self.store.list_names(), list(DEFAULT_CATEGORIES))

    def test_ensure_initialized_fills_empty_and_whitespace_files(self) -> None:
        for contents in ("", "\n  \n\t\n"):
            with self.subTest(contents=repr(contents)):
                self.store.path.write_text(contents, encoding="utf-8")
                self.store.ensure_initialized()
                self.assertEqual(self.store.list_names(), list(DEFAULT_CATEGORIES))

    def test_ensure_initialized_preserves_custom_categories(self) -> None:
        self.store.path.write_text('{"name": "coffee"}\n\n', encoding="utf-8")
        original = self.store.path.read_bytes()
        self.store.ensure_initialized()
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.store.list_names(), ["coffee"])

    def test_add_duplicate_returns_false(self) -> None:
        self.assertTrue(self.store.add("coffee"))
        self.assertFalse(self.store.add("coffee"))

    def test_remove(self) -> None:
        self.store.add("coffee")
        self.assertTrue(self.store.remove("coffee"))
        self.assertFalse(self.store.exists("coffee"))


class BudgetStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = BudgetStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_set_is_upsert(self) -> None:
        self.store.set("2024-01", 100)
        self.store.set("2024-01", 500)  # 갱신
        self.store.set("2024-02", 200)
        b = self.store.get("2024-01")
        assert b is not None
        self.assertEqual(b.amount, 500)
        self.assertEqual(len(list(self.store.stream())), 2)

    def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(self.store.get("1999-01"))


class InitStorageTest(unittest.TestCase):
    def test_creates_three_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            tx = TransactionRepository(base)
            cat = CategoryStore(base)
            bud = BudgetStore(base)
            init_storage(base, tx, cat, bud)
            self.assertTrue(tx.path.exists())
            self.assertTrue(cat.path.exists())
            self.assertTrue(bud.path.exists())


if __name__ == "__main__":
    unittest.main()
