"""저장소 계층 — 파일 I/O (JSONL) + 제너레이터 스트리밍.

핵심 포인트
-----------
- 조회는 파일 전체를 메모리에 올리지 않고 한 줄씩 yield 하는 '스트리밍'으로 처리한다.
- update/delete 는 임시 파일에 쓴 뒤 os.replace 로 '원자적 교체'하여 중간에 실패해도
  원본이 손상되지 않도록 한다.
- 저장 파일은 3개로 분리한다: transactions.jsonl / categories.jsonl / budgets.jsonl
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from .errors import StorageError
from .models import Budget, Category, Transaction

DEFAULT_CATEGORIES: tuple[str, ...] = ("food", "transport", "rent", "salary", "etc")


# ---------------------------------------------------------------------------
# 공통 저수준 I/O
# ---------------------------------------------------------------------------
def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """JSONL 파일을 한 줄씩 스트리밍으로 읽는다(파일 전체를 로드하지 않음)."""
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise StorageError(
                        f"{path.name} {line_no}행을 해석할 수 없습니다: {exc}",
                        "파일이 손상되었을 수 있습니다. 백업 후 해당 행을 확인하세요.",
                    )
    except OSError as exc:
        raise StorageError(
            f"{path.name} 파일을 열 수 없습니다: {exc}",
            "파일 경로와 읽기 권한을 확인하세요.",
        )


def _atomic_write_jsonl(
    path: Path,
    records: Iterable[dict[str, Any]],
    *,
    should_commit: Callable[[], bool] | None = None,
) -> None:
    """임시 파일 작성 후 조건을 확인하고 원본을 원자적으로 교체한다."""
    tmp_fd: int | None = None
    tmp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        f = os.fdopen(tmp_fd, "w", encoding="utf-8")
        tmp_fd = None  # 이제 파일 객체가 파일 디스크립터를 관리한다.
        with f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            # 조회와 임시 파일 작성을 한 번에 수행한 뒤, 변경 대상이 없으면 취소한다.
            if should_commit is not None and not should_commit():
                return
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)  # 원자적 교체
    except OSError as exc:
        raise StorageError(
            f"{path.name} 저장 중 오류가 발생했습니다: {exc}",
            "디스크 여유 공간과 쓰기 권한을 확인하세요.",
        ) from exc
    finally:
        # 레코드 생성/직렬화 오류와 사용자 중단도 임시 파일을 남기지 않는다.
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass  # 성공적으로 교체한 경우 임시 경로는 이미 없다.
            except OSError:
                pass


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """한 건을 추가하고 실패하면 원래 길이로 되돌린다(전체 파일 복사 없음)."""
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    original_size: int | None = None
    existed = path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 버퍼가 실패 후 close 때 다시 기록하는 일을 막기 위해 비버퍼 이진 I/O 사용.
        with path.open("a+b", buffering=0) as f:
            f.seek(0, os.SEEK_END)
            original_size = f.tell()
            if original_size:
                f.seek(-1, os.SEEK_END)
                # 마지막 레코드에 줄바꿈이 없어도 새 레코드와 붙지 않게 한다.
                # 추가한 구분자도 실패 시 original_size까지 함께 되돌린다.
                if f.read(1) not in (b"\n", b"\r"):
                    payload = b"\n" + payload
            remaining = memoryview(payload)
            while remaining:
                written = f.write(remaining)
                if not written:
                    raise OSError("파일에 데이터를 기록하지 못했습니다.")
                remaining = remaining[written:]
            f.flush()
            os.fsync(f.fileno())
    except BaseException as exc:
        rollback_error: OSError | None = None
        if original_size is not None:
            try:
                with path.open("r+b", buffering=0) as f:
                    f.truncate(original_size)
                    f.flush()
                    os.fsync(f.fileno())
                if not existed:
                    path.unlink()
            except OSError as rollback_exc:
                rollback_error = rollback_exc
        if isinstance(exc, OSError):
            detail = f"{path.name} 에 기록할 수 없습니다: {exc}"
            hint = "파일 경로와 쓰기 권한을 확인하세요."
            if rollback_error is not None:
                detail += f" (원상 복구 중 오류: {rollback_error})"
                hint = "디스크 여유 공간과 쓰기 권한을 확인하고 저장 파일을 점검하세요."
            raise StorageError(detail, hint) from exc
        if rollback_error is not None:
            raise StorageError(
                f"{path.name} 저장을 중단했지만 원상 복구 중 오류가 발생했습니다: {rollback_error}",
                "디스크 여유 공간과 쓰기 권한을 확인하고 저장 파일을 점검하세요.",
            ) from exc
        raise


# ---------------------------------------------------------------------------
# 거래 저장소
# ---------------------------------------------------------------------------
class TransactionRepository:
    """transactions.jsonl 을 다루는 저장소."""

    def __init__(self, data_dir: Path) -> None:
        self.path: Path = data_dir / "transactions.jsonl"

    def stream(self) -> Iterator[Transaction]:
        """모든 거래를 삽입 순서대로 스트리밍한다."""
        for rec in _read_jsonl(self.path):
            yield Transaction.from_dict(rec)

    def append(self, tx: Transaction) -> None:
        _append_jsonl(self.path, tx.to_dict())

    def append_many(self, transactions: Iterable[Transaction]) -> None:
        """추가 거래를 스트리밍하며 한 번에 저장한다. 중간 오류면 원본을 보존한다."""
        additions = iter(transactions)
        try:
            first = next(additions)
        except StopIteration:
            return

        def records() -> Iterator[dict[str, Any]]:
            for tx in self.stream():
                yield tx.to_dict()
            yield first.to_dict()
            for tx in additions:
                yield tx.to_dict()

        _atomic_write_jsonl(self.path, records())

    def get(self, tx_id: str) -> "Transaction | None":
        for tx in self.stream():
            if tx.id == tx_id:
                return tx
        return None

    def _max_number(self) -> int:
        """기존 거래 id 중 가장 큰 일련번호를 찾는다(예: TX-000012 -> 12)."""
        max_n = 0
        for tx in self.stream():
            if tx.id.startswith("TX-"):
                try:
                    max_n = max(max_n, int(tx.id[3:]))
                except ValueError:
                    continue
        return max_n

    def next_number(self) -> int:
        """다음에 사용할 일련번호."""
        return self._max_number() + 1

    def next_id(self) -> str:
        """다음에 사용할 거래 id 문자열(TX-000001 형식)."""
        return f"TX-{self.next_number():06d}"

    def rewrite(self, transactions: Iterable[Transaction]) -> None:
        """전체 거래를 원자적으로 다시 쓴다."""
        _atomic_write_jsonl(self.path, (t.to_dict() for t in transactions))

    def update(self, tx_id: str, changes: dict[str, Any]) -> Transaction:
        """id 로 거래를 찾아 변경분을 적용하고 원자적으로 저장한다."""
        updated: "Transaction | None" = None

        def gen() -> Iterator[dict[str, Any]]:
            nonlocal updated
            for tx in self.stream():
                if tx.id == tx_id:
                    data = tx.to_dict()
                    data.update(changes)
                    updated = Transaction.from_dict(data)
                    yield updated.to_dict()
                else:
                    yield tx.to_dict()

        _atomic_write_jsonl(self.path, gen(), should_commit=lambda: updated is not None)
        if updated is None:
            from .errors import NotFoundError

            raise NotFoundError(
                f"거래를 찾을 수 없습니다: {tx_id}",
                "list 명령으로 올바른 id를 확인하세요.",
            )
        return updated

    def delete(self, tx_id: str) -> bool:
        """id 로 거래를 삭제한다. 삭제되면 True, 없으면 False."""
        deleted = False

        def gen() -> Iterator[dict[str, Any]]:
            nonlocal deleted
            for tx in self.stream():
                if tx.id == tx_id:
                    deleted = True
                    continue
                yield tx.to_dict()

        _atomic_write_jsonl(self.path, gen(), should_commit=lambda: deleted)
        return deleted


# ---------------------------------------------------------------------------
# 카테고리 저장소
# ---------------------------------------------------------------------------
class CategoryStore:
    """categories.jsonl 을 다루는 저장소."""

    def __init__(self, data_dir: Path) -> None:
        self.path: Path = data_dir / "categories.jsonl"

    def ensure_initialized(self) -> None:
        """파일이 없거나 비어 있으면 기본 카테고리로 생성한다(안 A)."""
        if not any(self.stream()):
            _atomic_write_jsonl(
                self.path, ({"name": n} for n in DEFAULT_CATEGORIES)
            )

    def stream(self) -> Iterator[Category]:
        for rec in _read_jsonl(self.path):
            yield Category.from_dict(rec)

    def list_names(self) -> list[str]:
        return [c.name for c in self.stream()]

    def exists(self, name: str) -> bool:
        return any(c.name == name for c in self.stream())

    def add(self, name: str) -> bool:
        """카테고리를 추가한다. 이미 있으면 False."""
        if self.exists(name):
            return False
        _append_jsonl(self.path, {"name": name})
        return True

    def remove(self, name: str) -> bool:
        """카테고리를 삭제한다. 삭제되면 True."""
        removed = False

        def gen() -> Iterator[dict[str, Any]]:
            nonlocal removed
            for c in self.stream():
                if c.name == name:
                    removed = True
                    continue
                yield c.to_dict()

        _atomic_write_jsonl(self.path, gen())
        return removed


# ---------------------------------------------------------------------------
# 예산 저장소
# ---------------------------------------------------------------------------
class BudgetStore:
    """budgets.jsonl 을 다루는 저장소."""

    def __init__(self, data_dir: Path) -> None:
        self.path: Path = data_dir / "budgets.jsonl"

    def stream(self) -> Iterator[Budget]:
        for rec in _read_jsonl(self.path):
            yield Budget.from_dict(rec)

    def get(self, month: str) -> "Budget | None":
        result: "Budget | None" = None
        for b in self.stream():
            if b.month == month:
                result = b  # 같은 달이 여러 번 있으면 마지막 값 사용
        return result

    def set(self, month: str, amount: int) -> None:
        """월 예산을 upsert(없으면 추가, 있으면 갱신)하고 원자적으로 저장한다."""
        existing: dict[str, int] = {b.month: b.amount for b in self.stream()}
        existing[month] = amount
        _atomic_write_jsonl(
            self.path,
            ({"month": m, "amount": a} for m, a in existing.items()),
        )


# ---------------------------------------------------------------------------
# 초기화
# ---------------------------------------------------------------------------
def init_storage(
    data_dir: Path,
    tx_repo: TransactionRepository,
    cat_store: CategoryStore,
    budget_store: BudgetStore,
) -> None:
    """저장 폴더와 3개 파일을 준비한다(없으면 자동 생성)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    cat_store.ensure_initialized()
    for p in (tx_repo.path, budget_store.path):
        if not p.exists():
            p.touch()
