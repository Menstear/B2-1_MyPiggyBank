"""거래 날짜와 ID 순으로, 메모리 사용을 제한하며 정렬한다."""

from __future__ import annotations

import heapq
import json
from collections.abc import Iterable, Iterator
from contextlib import ExitStack
from itertools import count, islice
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TextIO

from .errors import StorageError, ValidationError
from .models import Transaction

# 100k 실측에서 5000건은 병합 패스를 줄이면서 10000건보다 메모리를 절약했다.
_CHUNK_SIZE = 5000
_MERGE_FAN_IN = 32
_IO_BUFFER_SIZE = 8 * 1024


def _sort_key(tx: Transaction) -> tuple[str, int, str]:
    """TX 번호는 자릿수가 늘어나도 숫자 순서로 비교한다."""
    suffix = tx.id[3:] if tx.id.startswith("TX-") else ""
    number = int(suffix) if suffix.isascii() and suffix.isdecimal() else -1
    return tx.date, number, tx.id


def newest_transactions(
    transactions: Iterable[Transaction], limit: int | None = None
) -> Iterator[Transaction]:
    """날짜 최신순, 같은 날에는 높은 TX 번호 순으로 거래를 반환한다.

    제한이 있으면 최대 limit건만 메모리에 보관한다. 제한이 없으면 한
    청크까지는 메모리에서 정렬하고, 이를 넘을 때만 임시 JSONL 파일로
    나누어 단계적으로 병합한다. 반환된 제너레이터를 끝까지 소비하거나
    close()하면 임시 파일을 정리한다.
    """
    if limit is not None:
        if limit < 0:
            raise ValidationError("조회 개수는 음수일 수 없습니다.", "0 이상의 개수를 지정하세요.")
        if limit == 0:
            return iter(())
        return _limited_transactions(transactions, limit)
    return _external_sort(transactions)


def _limited_transactions(
    transactions: Iterable[Transaction], limit: int
) -> Iterator[Transaction]:
    yield from heapq.nlargest(limit, transactions, key=_sort_key)


def _read_run(file: TextIO) -> Iterator[Transaction]:
    for line in file:
        yield Transaction.from_dict(json.loads(line))


def _merged_runs(paths: list[Path]) -> Iterator[Transaction]:
    # 병합하는 동안만 각 입력 파일을 열어 파일 핸들 수를 제한한다.
    with ExitStack() as stack:
        streams = [
            _read_run(stack.enter_context(path.open(
                "r", encoding="utf-8", buffering=_IO_BUFFER_SIZE
            )))
            for path in paths
        ]
        yield from heapq.merge(*streams, key=_sort_key, reverse=True)


def _write_run(path: Path, transactions: Iterable[Transaction]) -> None:
    with path.open("w", encoding="utf-8", buffering=_IO_BUFFER_SIZE) as file:
        for tx in transactions:
            file.write(json.dumps(tx.to_dict(), ensure_ascii=False) + "\n")


def _combine_runs(paths: list[Path], output: Path) -> None:
    # 쓰기 실패 시에도 입력 핸들을 즉시 닫는다.
    merged = _merged_runs(paths)
    try:
        _write_run(output, merged)
    finally:
        merged.close()
    for path in paths:
        path.unlink()


def _external_sort(transactions: Iterable[Transaction]) -> Iterator[Transaction]:
    try:
        source = iter(transactions)
        chunk = list(islice(source, _CHUNK_SIZE))
        try:
            overflow = next(source)
        except StopIteration:
            # 작은 결과는 디스크 권한/공간에 의존하지 않고 바로 정렬한다.
            chunk.sort(key=_sort_key, reverse=True)
            yield from chunk
            return

        with TemporaryDirectory(prefix="budget-sort-") as directory:
            base = Path(directory)
            sequence = count()
            levels: list[list[Path]] = []

            def new_path() -> Path:
                return base / f"{next(sequence)}.jsonl"

            def add_run(path: Path) -> None:
                # 각 단계에서 fan-in만큼 모이는 즉시 병합한다. 입력 건수가
                # 늘어도 거래와 임시 파일 경로를 전부 메모리에 쌓지 않는다.
                level = 0
                while True:
                    if level == len(levels):
                        levels.append([])
                    levels[level].append(path)
                    if len(levels[level]) < _MERGE_FAN_IN:
                        return
                    output = new_path()
                    _combine_runs(levels[level], output)
                    levels[level].clear()
                    path = output
                    level += 1

            def write_chunk() -> None:
                chunk.sort(key=_sort_key, reverse=True)
                path = new_path()
                _write_run(path, chunk)
                add_run(path)
                chunk.clear()

            write_chunk()
            chunk.append(overflow)
            del overflow
            for tx in source:
                if len(chunk) == _CHUNK_SIZE:
                    write_chunk()
                chunk.append(tx)
            if chunk:
                write_chunk()

            paths = [path for level in levels for path in level]
            while len(paths) > _MERGE_FAN_IN:
                reduced: list[Path] = []
                for offset in range(0, len(paths), _MERGE_FAN_IN):
                    group = paths[offset:offset + _MERGE_FAN_IN]
                    if len(group) == 1:
                        reduced.append(group[0])
                    else:
                        output = new_path()
                        _combine_runs(group, output)
                        reduced.append(output)
                paths = reduced
            yield from _merged_runs(paths)
    except OSError as exc:
        raise StorageError(
            f"거래 정렬용 임시 파일을 처리할 수 없습니다: {exc}",
            "임시 폴더의 공간과 쓰기 권한을 확인하세요.",
        ) from exc
