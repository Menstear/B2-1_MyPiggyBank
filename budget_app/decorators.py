"""데코레이터 계층 — 공통 관심사 분리.

- handle_errors : 예외를 잡아 '원인 + 힌트'만 출력하고 종료 코드를 설정(스택트레이스 금지)
- log_execution : 실행된 명령(함수명)을 로그로 남김
- measure_time  : 실행 시간을 측정해 로그로 남김

로그는 기본적으로 stderr의 WARNING 레벨이라 평소엔 조용하고,
--verbose 옵션을 주면 DEBUG 레벨까지 출력되어 동작을 확인할 수 있다.
"""

from __future__ import annotations

import functools
import logging
import sys
import time
from typing import Any, Callable, TypeVar

from .errors import BudgetError
from .output import OutputError

F = TypeVar("F", bound=Callable[..., Any])

logger = logging.getLogger("budget_app")


def handle_errors(func: F) -> F:
    """커스텀 예외를 '원인 + 힌트'로 출력하고 알맞은 종료 코드로 종료한다."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except (OutputError, BrokenPipeError):
            # 출력 장애는 같은 채널에 재출력하지 않고 진입점에서 처리한다.
            raise
        except BudgetError as exc:
            print(f"[오류] {exc.message}")
            if exc.hint:
                print(f"[힌트] {exc.hint}")
            sys.exit(exc.exit_code)
        except (KeyboardInterrupt, EOFError):
            print()
            print("[중단] 작업이 취소되었습니다.")
            sys.exit(130)
        except Exception as exc:  # noqa: BLE001 - 최종 안전망(스택트레이스 노출 방지)
            print(f"[오류] 예기치 못한 오류가 발생했습니다: {exc}")
            print("[힌트] 입력값과 ./data 파일 상태를 확인해 주세요.")
            sys.exit(1)

    return wrapper  # type: ignore[return-value]


def log_execution(func: F) -> F:
    """실행된 명령(함수명)을 로그로 남긴다."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        logger.info("실행: %s", func.__name__)
        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def measure_time(func: F) -> F:
    """함수 실행 시간을 측정해 로그로 남긴다(예외가 나도 측정)."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.debug("%s 실행 시간: %.2f ms", func.__name__, elapsed_ms)

    return wrapper  # type: ignore[return-value]
