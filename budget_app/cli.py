"""CLI 계층 — argparse 파서, 명령 핸들러, 대화형 입력/출력.

모든 핸들러에는 공통 관심사 데코레이터(예외/로그/시간 측정)를 적용한다.
성공 시 0을 반환하고, 오류는 handle_errors 가 '원인 + 힌트'로 출력 후 종료한다.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

from .decorators import handle_errors, log_execution, measure_time
from .errors import ValidationError
from .repository import (
    BudgetStore,
    CategoryStore,
    TransactionRepository,
    init_storage,
)
from .service import BudgetService, SearchQuery
from .models import Transaction
from .validators import (
    parse_tags,
    validate_amount,
    validate_date,
    validate_month,
    validate_type,
)

DEFAULT_DATA_DIR = "./data"
DEFAULT_LIMIT = 20
DEFAULT_TOP = 5


# ---------------------------------------------------------------------------
# 서비스 조립 / 출력 헬퍼
# ---------------------------------------------------------------------------
def build_service(data_dir: str) -> BudgetService:
    base = Path(data_dir)
    tx_repo = TransactionRepository(base)
    cat_store = CategoryStore(base)
    budget_store = BudgetStore(base)
    init_storage(base, tx_repo, cat_store, budget_store)
    return BudgetService(tx_repo, cat_store, budget_store)


def format_tx_line(tx: Transaction) -> str:
    return (
        f"{tx.id} | {tx.date} | {tx.type} | {tx.category} | {tx.amount} | {tx.memo}"
    )


def prompt_valid(label: str, validator: Callable[[str], object]) -> object:
    """검증을 통과할 때까지 재입력을 받는 대화형 프롬프트."""
    while True:
        raw = input(label)
        try:
            return validator(raw)
        except ValidationError as exc:
            print(f"[오류] {exc.message}")
            if exc.hint:
                print(f"[힌트] {exc.hint}")


def prompt_category(service: BudgetService) -> str:
    """등록된 카테고리를 입력할 때까지 재입력을 받는다."""
    while True:
        name = input("카테고리: ").strip()
        if service.categories.exists(name):
            return name
        print(f"[오류] 등록되지 않은 카테고리입니다: {name!r}")
        print(f"[힌트] 등록된 카테고리: {', '.join(service.list_categories())}")


# ---------------------------------------------------------------------------
# 명령 핸들러
# ---------------------------------------------------------------------------
@handle_errors
@log_execution
@measure_time
def cmd_add(args: argparse.Namespace) -> int:
    service = build_service(args.data_dir)
    print("거래를 추가합니다. (취소: Ctrl+C)")
    date = prompt_valid("날짜(YYYY-MM-DD): ", validate_date)
    type_ = prompt_valid("타입(income/expense): ", validate_type)
    category = prompt_category(service)
    amount = prompt_valid("금액(양수): ", validate_amount)
    memo = input("메모(선택): ").strip()
    tags = parse_tags(input("태그(쉼표로 구분, 없으면 엔터): ").strip())
    tx = service.add_transaction(
        type=str(type_),
        date=str(date),
        amount=int(amount),  # type: ignore[arg-type]
        category=category,
        memo=memo,
        tags=tags,
    )
    print(f"[저장 완료] id={tx.id}")
    return 0


@handle_errors
@log_execution
@measure_time
def cmd_list(args: argparse.Namespace) -> int:
    service = build_service(args.data_dir)
    txs = service.list_transactions(args.limit)
    if not txs:
        print("거래 내역이 없습니다.")
        return 0
    for tx in txs:
        print(format_tx_line(tx))
    return 0


@handle_errors
@log_execution
@measure_time
def cmd_search(args: argparse.Namespace) -> int:
    service = build_service(args.data_dir)
    query = SearchQuery(
        date_from=validate_date(args.date_from) if args.date_from else None,
        date_to=validate_date(args.date_to) if args.date_to else None,
        category=args.category,
        type=validate_type(args.type) if args.type else None,
        q=args.q,
        tag=args.tag,
    )
    results = service.search(query, limit=args.limit)
    found = False
    try:
        for tx in results:
            found = True
            print(format_tx_line(tx))
    finally:
        close = getattr(results, "close", None)
        if close is not None:
            close()  # 출력 중 취소되더라도 정렬용 임시 파일을 즉시 정리한다.
    if not found:
        print("검색 결과가 없습니다.")
    return 0


@handle_errors
@log_execution
@measure_time
def cmd_summary(args: argparse.Namespace) -> int:
    service = build_service(args.data_dir)
    s = service.summary(args.month)
    if s["count"] == 0:
        print(f"{s['month']} 데이터 없음")
    print(f"총 수입: {s['total_income']}원")
    print(f"총 지출: {s['total_expense']}원")
    print(f"잔액: {s['balance']}원")
    budget = s["budget"]
    if budget is not None:
        rate = (s["total_expense"] / budget.amount * 100) if budget.amount else 0.0
        print(f"예산: {budget.amount}원 (사용률 {rate:.1f}%)")
        if s["total_expense"] > budget.amount:
            over = s["total_expense"] - budget.amount
            print(f"[경고] 예산을 {over}원 초과했습니다!")
    top = s["expense_by_category"].most_common(args.top)
    if top:
        print()
        print(f"지출 TOP {args.top}")
        for i, (cat, amt) in enumerate(top, start=1):
            print(f"{i}) {cat} {amt}원")
    return 0


@handle_errors
@log_execution
@measure_time
def cmd_budget(args: argparse.Namespace) -> int:
    service = build_service(args.data_dir)
    if args.budget_cmd == "set":
        b = service.set_budget(args.month, args.amount)
        print(f"[저장 완료] {b.month} 예산 {b.amount}원")
    return 0


@handle_errors
@log_execution
@measure_time
def cmd_category(args: argparse.Namespace) -> int:
    service = build_service(args.data_dir)
    if args.cat_cmd == "add":
        name = args.name if args.name else input("카테고리명: ").strip()
        service.add_category(name)
        print(f"[저장 완료] category={name}")
    elif args.cat_cmd == "list":
        names = service.list_categories()
        if not names:
            print("등록된 카테고리가 없습니다.")
        for n in names:
            print(f"- {n}")
    elif args.cat_cmd == "remove":
        moved = service.remove_category(args.name, args.replace)
        if moved:
            print(
                f"[삭제 완료] category={args.name} "
                f"(사용 내역 {moved}건을 '{args.replace}'로 변경)"
            )
        else:
            print(f"[삭제 완료] category={args.name}")
    return 0


@handle_errors
@log_execution
@measure_time
def cmd_update(args: argparse.Namespace) -> int:
    service = build_service(args.data_dir)
    changes: dict[str, object] = {}
    if args.date is not None:
        changes["date"] = args.date
    if args.type is not None:
        changes["type"] = args.type
    if args.amount is not None:
        changes["amount"] = args.amount
    if args.category is not None:
        changes["category"] = args.category
    if args.memo is not None:
        changes["memo"] = args.memo
    if args.tags is not None:
        changes["tags"] = parse_tags(args.tags)
    tx = service.update_transaction(args.tx_id, changes)
    print(f"[수정 완료] id={tx.id}")
    return 0


@handle_errors
@log_execution
@measure_time
def cmd_delete(args: argparse.Namespace) -> int:
    service = build_service(args.data_dir)
    service.delete_transaction(args.tx_id)
    print(f"[삭제 완료] id={args.tx_id}")
    return 0


@handle_errors
@log_execution
@measure_time
def cmd_export(args: argparse.Namespace) -> int:
    service = build_service(args.data_dir)
    if not args.month and not (args.date_from or args.date_to):
        raise ValidationError(
            "export 조건이 없습니다.",
            "--month YYYY-MM 또는 --from/--to 중 하나 이상을 지정하세요.",
        )
    if args.month:
        month = validate_month(args.month)
        query = SearchQuery(date_from=f"{month}-01", date_to=f"{month}-31")
    else:
        query = SearchQuery(
            date_from=validate_date(args.date_from) if args.date_from else None,
            date_to=validate_date(args.date_to) if args.date_to else None,
        )
    n = service.export_csv(Path(args.out), query)
    print(f"[완료] {args.out} ({n} records)")
    return 0


@handle_errors
@log_execution
@measure_time
def cmd_import(args: argparse.Namespace) -> int:
    service = build_service(args.data_dir)
    report = service.import_csv_report(Path(args.src))
    print(f"[완료] imported={report.imported}, skipped={report.skipped}")
    for issue in report.samples:
        print(f"[건너뜀] CSV {issue.line}행: {issue.message}")
        if issue.hint:
            print(f"[힌트] {issue.hint}")
    remaining = report.skipped - len(report.samples)
    if remaining:
        print(f"[안내] 나머지 {remaining}건의 상세 내역은 생략했습니다.")
    return 0


# ---------------------------------------------------------------------------
# 파서 구성
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    def add_common_options(
        target: argparse.ArgumentParser, *, suppress_defaults: bool
    ) -> None:
        target.add_argument(
            "--data-dir",
            dest="data_dir",
            default=argparse.SUPPRESS if suppress_defaults else DEFAULT_DATA_DIR,
            help=f"저장 폴더 경로 (기본: {DEFAULT_DATA_DIR})",
        )
        target.add_argument(
            "--verbose",
            action="store_true",
            default=argparse.SUPPRESS if suppress_defaults else False,
            help="실행 로그/시간 측정을 stderr로 출력",
        )

    # 하위 파서에 옵션이 없으면 앞서 파싱한 값을 그대로 유지한다.
    common = argparse.ArgumentParser(add_help=False)
    add_common_options(common, suppress_defaults=True)

    parser = argparse.ArgumentParser(
        prog="budget_app",
        description="파일 기반 콘솔 가계부 (표준 라이브러리만 사용)",
    )
    # parents의 Action은 공유되므로 루트 기본값은 별도 Action에 설정한다.
    add_common_options(parser, suppress_defaults=False)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # add
    p_add = sub.add_parser("add", parents=[common], help="거래 추가(대화형)")
    p_add.set_defaults(func=cmd_add)

    # list
    p_list = sub.add_parser("list", parents=[common], help="거래 목록(거래 날짜 최신순)")
    p_list.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"출력 개수 (기본: {DEFAULT_LIMIT})")
    p_list.set_defaults(func=cmd_list)

    # search
    p_search = sub.add_parser("search", parents=[common], help="거래 검색(거래 날짜 최신순)")
    p_search.add_argument("--from", dest="date_from", help="시작일 YYYY-MM-DD")
    p_search.add_argument("--to", dest="date_to", help="종료일 YYYY-MM-DD")
    p_search.add_argument("--category", help="카테고리")
    p_search.add_argument("--type", help="income / expense")
    p_search.add_argument("--q", help="메모 키워드")
    p_search.add_argument("--tag", help="태그")
    p_search.add_argument("--limit", type=int, default=None, help="출력 개수")
    p_search.set_defaults(func=cmd_search)

    # summary
    p_sum = sub.add_parser("summary", parents=[common], help="월별 요약")
    p_sum.add_argument("--month", required=True, help="대상 월 YYYY-MM")
    p_sum.add_argument("--top", type=int, default=DEFAULT_TOP,
                       help=f"지출 TOP N (기본: {DEFAULT_TOP})")
    p_sum.set_defaults(func=cmd_summary)

    # budget
    p_budget = sub.add_parser("budget", parents=[common], help="예산 설정")
    budget_sub = p_budget.add_subparsers(dest="budget_cmd", metavar="<action>",
                                         required=True)
    p_budget_set = budget_sub.add_parser("set", parents=[common], help="월 예산 설정")
    p_budget_set.add_argument("--month", required=True, help="대상 월 YYYY-MM")
    p_budget_set.add_argument("--amount", required=True, help="예산 금액(양수)")
    p_budget.set_defaults(func=cmd_budget)

    # category
    p_cat = sub.add_parser("category", parents=[common], help="카테고리 관리")
    cat_sub = p_cat.add_subparsers(dest="cat_cmd", metavar="<action>", required=True)
    p_cat_add = cat_sub.add_parser("add", parents=[common], help="카테고리 추가")
    p_cat_add.add_argument("name", nargs="?", help="카테고리명(생략 시 대화형)")
    cat_sub.add_parser("list", parents=[common], help="카테고리 목록")
    p_cat_rm = cat_sub.add_parser("remove", parents=[common], help="카테고리 삭제")
    p_cat_rm.add_argument("name", help="삭제할 카테고리명")
    p_cat_rm.add_argument("--replace", default=None, help="사용 중일 때 대체 카테고리")
    p_cat.set_defaults(func=cmd_category)

    # update (옵션 기반으로 고정)
    p_up = sub.add_parser("update", parents=[common], help="거래 수정(옵션 기반)")
    p_up.add_argument("--id", dest="tx_id", required=True, help="수정할 거래 id")
    p_up.add_argument("--date", help="YYYY-MM-DD")
    p_up.add_argument("--type", help="income / expense")
    p_up.add_argument("--category", help="카테고리")
    p_up.add_argument("--amount", help="금액(양수)")
    p_up.add_argument("--memo", help="메모")
    p_up.add_argument("--tags", help="태그(쉼표 구분)")
    p_up.set_defaults(func=cmd_update)

    # delete
    p_del = sub.add_parser("delete", parents=[common], help="거래 삭제")
    p_del.add_argument("--id", dest="tx_id", required=True, help="삭제할 거래 id")
    p_del.set_defaults(func=cmd_delete)

    # export
    p_exp = sub.add_parser("export", parents=[common], help="CSV 내보내기")
    p_exp.add_argument("--out", required=True, help="출력 CSV 경로")
    p_exp.add_argument("--month", help="대상 월 YYYY-MM")
    p_exp.add_argument("--from", dest="date_from", help="시작일 YYYY-MM-DD")
    p_exp.add_argument("--to", dest="date_to", help="종료일 YYYY-MM-DD")
    p_exp.set_defaults(func=cmd_export)

    # import
    p_imp = sub.add_parser("import", parents=[common], help="CSV 가져오기")
    p_imp.add_argument("--from", dest="src", required=True, help="입력 CSV 경로")
    p_imp.set_defaults(func=cmd_import)

    return parser
