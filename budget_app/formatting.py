"""입력 제한을 바꾸지 않고 계산 결과를 십진수로 표시한다."""

from __future__ import annotations


def format_integer(value: int) -> str:
    """일반 정수는 바로 표시하고, 큰 합계는 9자리씩 나누어 표시한다.

    각각 유효한 금액도 합산하면 Python의 정수 문자열 변환 자릿수 제한을
    넘을 수 있다. 전역 제한은 유지하고 출력할 값만 작은 조각으로 변환한다.
    """
    try:
        return str(value)
    except ValueError:
        negative = value < 0
        remaining = abs(value)
        chunks: list[str] = []
        while remaining >= 1_000_000_000:
            remaining, chunk = divmod(remaining, 1_000_000_000)
            chunks.append(f"{chunk:09d}")
        chunks.append(str(remaining))
        return ("-" if negative else "") + "".join(reversed(chunks))
