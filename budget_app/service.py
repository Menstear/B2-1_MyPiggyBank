"""서비스 계층 — 비즈니스 로직.

CLI(입출력)와 저장소(파일 I/O) 사이에서 규칙을 담당한다.
검증, 검색 필터링, 월별 집계, 예산 계산, 카테고리 사용 검사, CSV 입출력.
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .errors import (
    CategoryError,
    ImportExportError,
    NotFoundError,
    ValidationError,
)
from .models import Budget, Transaction
from .ordering import newest_transactions
from .repository import BudgetStore, CategoryStore, TransactionRepository
from .validators import (
    parse_tags,
    validate_amount,
    validate_date,
    validate_month,
    validate_type,
)

CSV_HEADER: list[str] = ["date", "type", "category", "amount", "memo", "tags"]
CSV_REQUIRED: set[str] = {"date", "type", "category", "amount"}
_IMPORT_SAMPLE_LIMIT = 5


@dataclass(frozen=True)
class ImportIssue:
    """건너뛴 CSV 레코드의 마지막 물리적 행 번호와 원인/해결 힌트."""

    line: int
    message: str
    hint: str


@dataclass(frozen=True)
class ImportReport:
    """저장 완료된 가져오기 결과. 오류 샘플은 최대 5건만 보관한다."""

    imported: int
    skipped: int
    samples: tuple[ImportIssue, ...] = ()


@contextmanager
def _large_csv_fields() -> Iterator[None]:
    """자체 내보낸 긴 문자열도 읽고 CSV 전역 설정은 원래대로 복구한다."""
    previous = csv.field_size_limit()
    candidate = sys.maxsize
    try:
        while True:
            try:
                csv.field_size_limit(candidate)
                break
            except OverflowError:  # 일부 플랫폼의 C long 범위에 맞춘다.
                candidate //= 10
        yield
    finally:
        csv.field_size_limit(previous)


@dataclass
class SearchQuery:
    """생성 시 날짜와 타입을 한 번 검증/정규화하는 검색·필터 조건."""

    date_from: Optional[str] = None
    date_to: Optional[str] = None
    category: Optional[str] = None
    type: Optional[str] = None
    q: Optional[str] = None       # 메모 키워드
    tag: Optional[str] = None

    def __post_init__(self) -> None:
        if self.date_from is not None:
            self.date_from = validate_date(self.date_from)
        if self.date_to is not None:
            self.date_to = validate_date(self.date_to)
        if self.type is not None:
            self.type = validate_type(self.type)
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValidationError(
                "검색 시작일이 종료일보다 늦습니다.",
                "--from 날짜가 --to 날짜보다 늦지 않도록 지정하세요.",
            )

    def matches(self, tx: Transaction) -> bool:
        # 날짜는 'YYYY-MM-DD' 라 문자열 비교로 대소 판정이 가능하다.
        if self.date_from and tx.date < self.date_from:
            return False
        if self.date_to and tx.date > self.date_to:
            return False
        if self.category and tx.category != self.category:
            return False
        if self.type and tx.type != self.type:
            return False
        if self.q and self.q.lower() not in tx.memo.lower():
            return False
        if self.tag and self.tag not in tx.tags:
            return False
        return True


class BudgetService:
    """가계부의 핵심 유스케이스를 제공하는 서비스."""

    def __init__(
        self,
        tx_repo: TransactionRepository,
        cat_store: CategoryStore,
        budget_store: BudgetStore,
    ) -> None:
        self.tx_repo = tx_repo
        self.categories = cat_store
        self.budgets = budget_store

    # ------------------------------------------------------------------ add
    def add_transaction(
        self,
        *,
        type: str,
        date: str,
        amount: "str | int",
        category: str,
        memo: str = "",
        tags: Optional[list[str]] = None,
    ) -> Transaction:
        type_ = validate_type(type)
        date_ = validate_date(date)
        amount_ = validate_amount(amount)
        if not self.categories.exists(category):
            raise CategoryError(
                f"등록되지 않은 카테고리입니다: {category!r}",
                "category add 로 추가하거나 category list 로 확인하세요.",
            )
        tx = Transaction(
            id=self.tx_repo.next_id(),
            type=type_,
            date=date_,
            amount=amount_,
            category=category,
            memo=memo or "",
            tags=tags or [],
        )
        self.tx_repo.append(tx)
        return tx

    # ----------------------------------------------------------------- list
    def list_transactions(self, limit: int) -> list[Transaction]:
        """거래 날짜 최신순으로 최대 limit개. 같은 날짜는 높은 TX 번호 순."""
        return list(newest_transactions(self.tx_repo.stream(), limit))

    # --------------------------------------------------------------- search
    def search(
        self, query: SearchQuery, limit: Optional[int] = None
    ) -> Iterator[Transaction]:
        """조건에 맞는 거래를 날짜 최신순으로 스트리밍한다."""
        matched: Iterator[Transaction] = (
            tx for tx in self.tx_repo.stream() if query.matches(tx)
        )
        yield from newest_transactions(matched, limit)

    # -------------------------------------------------------------- summary
    def summary(self, month: str) -> dict[str, Any]:
        month_ = validate_month(month)
        total_income = 0
        total_expense = 0
        expense_by_cat: "Counter[str]" = Counter()
        count = 0
        for tx in self.tx_repo.stream():  # 해당 월만 스트리밍 집계
            if tx.date[:7] != month_:
                continue
            count += 1
            if tx.type == "income":
                total_income += tx.amount
            else:
                total_expense += tx.amount
                expense_by_cat[tx.category] += tx.amount
        return {
            "month": month_,
            "count": count,
            "total_income": total_income,
            "total_expense": total_expense,
            "balance": total_income - total_expense,
            "expense_by_category": expense_by_cat,
            "budget": self.budgets.get(month_),
        }

    # --------------------------------------------------------------- budget
    def set_budget(self, month: str, amount: "str | int") -> Budget:
        month_ = validate_month(month)
        amount_ = validate_amount(amount)
        self.budgets.set(month_, amount_)
        return Budget(month=month_, amount=amount_)

    # ------------------------------------------------------------- category
    def list_categories(self) -> list[str]:
        return self.categories.list_names()

    def add_category(self, name: str) -> None:
        clean = (name or "").strip()
        if not clean:
            raise ValidationError("카테고리명이 비어 있습니다.", "예: food")
        if not self.categories.add(clean):
            raise CategoryError(
                f"이미 존재하는 카테고리입니다: {clean}",
                "category list 로 현재 목록을 확인하세요.",
            )

    def remove_category(self, name: str, replacement: Optional[str] = None) -> int:
        """카테고리를 삭제한다. 사용 중이면 대체 카테고리 지정을 요구한다.

        반환값: 대체 카테고리로 재분류된 거래 수(사용 중이 아니면 0).
        """
        if not self.categories.exists(name):
            raise NotFoundError(
                f"존재하지 않는 카테고리입니다: {name}",
                "category list 로 확인하세요.",
            )
        in_use = sum(1 for tx in self.tx_repo.stream() if tx.category == name)
        if in_use:
            if replacement is None:
                raise CategoryError(
                    f"'{name}' 카테고리를 사용하는 거래가 {in_use}건 있어 삭제할 수 없습니다.",
                    "--replace <카테고리> 로 대체 카테고리를 지정하세요.",
                )
            if replacement == name:
                raise CategoryError(
                    "대체 카테고리가 삭제 대상과 동일합니다.",
                    "다른 카테고리명을 지정하세요.",
                )
            if not self.categories.exists(replacement):
                raise CategoryError(
                    f"대체 카테고리가 존재하지 않습니다: {replacement}",
                    "category add 로 먼저 추가하거나 category list 로 확인하세요.",
                )

            def reassigned() -> Iterator[Transaction]:
                for tx in self.tx_repo.stream():
                    if tx.category == name:
                        tx.category = replacement
                    yield tx

            self.tx_repo.rewrite(reassigned())
        self.categories.remove(name)
        return in_use

    # --------------------------------------------------------------- update
    def update_transaction(
        self, tx_id: str, changes: dict[str, Any]
    ) -> Transaction:
        """옵션 기반 수정. 제공된 필드만 검증 후 적용한다."""
        clean: dict[str, Any] = {}
        if changes.get("date") is not None:
            clean["date"] = validate_date(changes["date"])
        if changes.get("type") is not None:
            clean["type"] = validate_type(changes["type"])
        if changes.get("amount") is not None:
            clean["amount"] = validate_amount(changes["amount"])
        if changes.get("category") is not None:
            cat = changes["category"]
            if not self.categories.exists(cat):
                raise CategoryError(
                    f"등록되지 않은 카테고리입니다: {cat!r}",
                    "category list 로 확인하세요.",
                )
            clean["category"] = cat
        if changes.get("memo") is not None:
            clean["memo"] = changes["memo"]
        if changes.get("tags") is not None:
            clean["tags"] = changes["tags"]
        if not clean:
            raise ValidationError(
                "수정할 항목이 없습니다.",
                "--date/--type/--amount/--category/--memo/--tags 중 하나 이상을 지정하세요.",
            )
        return self.tx_repo.update(tx_id, clean)

    # --------------------------------------------------------------- delete
    def delete_transaction(self, tx_id: str) -> None:
        if not self.tx_repo.delete(tx_id):
            raise NotFoundError(
                f"거래를 찾을 수 없습니다: {tx_id}",
                "list 명령으로 올바른 id를 확인하세요.",
            )

    # --------------------------------------------------------------- export
    def _check_export_path(self, out_path: Path) -> None:
        """직접 경로와 심볼릭/하드 링크 모두 내부 저장 파일을 보호한다."""
        for stored in (self.tx_repo.path, self.categories.path, self.budgets.path):
            try:
                same_path = out_path.resolve() == stored.resolve()
                same_file = (
                    out_path.exists() and stored.exists() and out_path.samefile(stored)
                )
            except (OSError, RuntimeError) as exc:
                raise ImportExportError(
                    f"출력 경로를 확인할 수 없습니다: {out_path} ({exc})",
                    "경로와 접근 권한을 확인하고 순환하는 심볼릭 링크를 피하세요.",
                ) from exc
            if same_path or same_file:
                raise ImportExportError(
                    f"내부 저장 파일로 내보낼 수 없습니다: {out_path}",
                    "transactions/categories/budgets 저장 파일과 다른 CSV 경로를 지정하세요.",
                )

    def export_csv(self, out_path: Path, query: SearchQuery) -> int:
        tmp_fd: int | None = None
        tmp_name: str | None = None
        count = 0
        try:
            self._check_export_path(out_path)
            # 출력 실패로 기존 CSV도 손상되지 않도록 같은 폴더에서 준비한다.
            tmp_fd, tmp_name = tempfile.mkstemp(dir=out_path.parent, suffix=".tmp")
            f = os.fdopen(tmp_fd, "w", encoding="utf-8", newline="")
            tmp_fd = None
            with f:
                writer = csv.writer(f)
                writer.writerow(CSV_HEADER)
                for tx in self.tx_repo.stream():
                    if not query.matches(tx):
                        continue
                    writer.writerow(
                        [
                            tx.date,
                            tx.type,
                            tx.category,
                            tx.amount,
                            tx.memo,
                            ",".join(tx.tags),
                        ]
                    )
                    count += 1
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, out_path)
        except (OSError, UnicodeError, csv.Error) as exc:
            raise ImportExportError(
                f"{out_path} 에 쓸 수 없습니다: {exc}",
                "출력 경로와 쓰기 권한을 확인하세요.",
            ) from exc
        finally:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
        return count

    # --------------------------------------------------------------- import
    def import_csv(self, in_path: Path) -> tuple[int, int]:
        """기존 API: CSV를 일괄 저장하고 (imported, skipped)를 반환한다."""
        report = self.import_csv_report(in_path)
        return report.imported, report.skipped

    def import_csv_report(self, in_path: Path) -> ImportReport:
        """원자적으로 가져오고 건너뛴 행의 번호/원인을 최대 5건 보고한다."""
        if not in_path.exists():
            raise ImportExportError(
                f"가져올 파일을 찾을 수 없습니다: {in_path}",
                "경로가 올바른지 확인하세요.",
            )
        imported = 0
        skipped = 0
        samples: list[ImportIssue] = []
        counter: int | None = None  # 첫 정상 행에서만 ID를 조회한 뒤 메모리에서 증가
        try:
            with _large_csv_fields(), in_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f, strict=True)
                fieldnames = reader.fieldnames or []
                duplicates = sorted(
                    name for name, count in Counter(fieldnames).items() if count > 1
                )
                if duplicates:
                    raise ImportExportError(
                        f"CSV 헤더에 중복 컬럼이 있습니다: {', '.join(duplicates)}",
                        "컬럼명을 각각 한 번만 쓰세요. 필수 컬럼: date,type,category,amount",
                    )
                fields = set(fieldnames)
                if not CSV_REQUIRED.issubset(fields):
                    missing = ", ".join(sorted(CSV_REQUIRED - fields))
                    raise ImportExportError(
                        f"CSV 헤더에 필수 컬럼이 없습니다: {missing}",
                        "필수 컬럼: date,type,category,amount (memo,tags 선택)",
                    )
                # 한 번의 가져오기에서 사용할 카테고리를 한 번만 읽는다.
                # 행마다 파일을 열고 선형 탐색하던 비용을 O(C + N)으로 줄인다.
                known_categories = set(self.categories.list_names())

                def valid_transactions() -> Iterator[Transaction]:
                    nonlocal counter, imported, skipped
                    try:
                        for row in reader:
                            try:
                                if None in row:
                                    raise ValidationError(
                                        "CSV 값의 개수가 헤더보다 많습니다.",
                                        "금액에는 천 단위 쉼표를 쓰지 말고, 메모의 쉼표는 따옴표로 감싸세요.",
                                    )
                                missing_values = sorted(
                                    name for name in CSV_REQUIRED if row.get(name) is None
                                )
                                if missing_values:
                                    raise ValidationError(
                                        f"CSV 행에 필수 컬럼 값이 없습니다: {', '.join(missing_values)}",
                                        "헤더 순서에 맞춰 date,type,category,amount 값을 모두 입력하세요.",
                                    )
                                type_ = validate_type(row.get("type", ""))
                                date_ = validate_date(row.get("date", ""))
                                amount_ = validate_amount(row.get("amount", ""))
                                category_ = (row.get("category") or "").strip()
                                if category_ not in known_categories:
                                    raise CategoryError(
                                        f"등록되지 않은 카테고리입니다: {category_!r}",
                                        "category add 로 추가하거나 등록된 카테고리로 수정하세요.",
                                    )
                            except (ValidationError, CategoryError) as exc:
                                skipped += 1
                                if len(samples) < _IMPORT_SAMPLE_LIMIT:
                                    samples.append(ImportIssue(reader.line_num, exc.message, exc.hint))
                                continue
                            if counter is None:
                                counter = self.tx_repo.next_number()
                            tx = Transaction(
                                id=f"TX-{counter:06d}",
                                type=type_,
                                date=date_,
                                amount=amount_,
                                category=category_,
                                memo=row.get("memo") or "",
                                tags=parse_tags(row.get("tags") or ""),
                            )
                            counter += 1
                            imported += 1
                            yield tx
                    except (OSError, UnicodeError, csv.Error) as exc:
                        raise ImportExportError(
                            f"{in_path} CSV {reader.line_num}행 부근을 읽을 수 없습니다: {exc}",
                            "UTF-8 인코딩과 CSV의 따옴표, 헤더를 확인하세요. 거래는 저장되지 않았으므로 수정 후 다시 실행하세요.",
                        ) from exc

                self.tx_repo.append_many(valid_transactions())
        except (OSError, UnicodeError, csv.Error) as exc:
            raise ImportExportError(
                f"{in_path} 를 읽을 수 없습니다: {exc}",
                "파일 경로, 읽기 권한, UTF-8 인코딩과 CSV 형식을 확인하세요. 거래는 저장되지 않았습니다.",
            ) from exc
        return ImportReport(imported, skipped, tuple(samples))
