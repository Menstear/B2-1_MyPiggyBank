"""입력 검증 계층.

검증 실패 시 ValidationError(원인 + 힌트)를 던진다.
대화형 입력에서는 재입력을 유도하고, 옵션/CSV 입력에서는 오류로 처리한다.
"""

from __future__ import annotations

import datetime as dt

from .errors import ValidationError

VALID_TYPES: tuple[str, str] = ("income", "expense")


def validate_date(value: str) -> str:
    """'YYYY-MM-DD' 형식 검증. 올바르면 zero-pad 정규화한 문자열을 반환.

    strptime 은 '2024-1-5' 같은 비정규 입력도 통과시키므로,
    date.isoformat 으로 '2024-01-05' 로 정규화한다.
    (정규화하지 않으면 월 매칭 date[:7] 과 날짜 문자열 비교가 틀어진다.)
    """
    text = (value or "").strip()
    try:
        parsed = dt.datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise ValidationError(
            f"날짜 형식이 올바르지 않습니다: {text!r}",
            "예: 2024-01-15 (YYYY-MM-DD)",
        )
    return parsed.date().isoformat()


def validate_month(value: str) -> str:
    """'YYYY-MM' 형식 검증. 올바르면 zero-pad 정규화한 문자열을 반환."""
    text = (value or "").strip()
    try:
        parsed = dt.datetime.strptime(text, "%Y-%m")
    except ValueError:
        raise ValidationError(
            f"월 형식이 올바르지 않습니다: {text!r}",
            "예: 2024-01 (YYYY-MM)",
        )
    return f"{parsed.year:04d}-{parsed.month:02d}"


def validate_type(value: str) -> str:
    """거래 타입 검증. income / expense 만 허용."""
    text = (value or "").strip().lower()
    if text not in VALID_TYPES:
        raise ValidationError(
            f"허용되지 않은 타입입니다: {value!r}",
            "income 또는 expense 중 하나를 입력하세요.",
        )
    return text


def validate_amount(value: "str | int") -> int:
    """금액 검증. 양수 정수만 허용."""
    text = str(value).strip()
    try:
        amount = int(text)
    except ValueError:
        raise ValidationError(
            f"금액은 정수여야 합니다: {value!r}",
            "예: 15000",
        )
    if amount <= 0:
        raise ValidationError(
            f"금액은 양수여야 합니다: {amount}",
            "0보다 큰 정수를 입력하세요.",
        )
    return amount


def parse_tags(value: str) -> list[str]:
    """쉼표로 구분된 태그 문자열을 리스트로 변환(빈 값 제거)."""
    if not value:
        return []
    return [t.strip() for t in value.split(",") if t.strip()]
