"""사용자 친화 예외 계층.

모든 예외는 '원인(message)'과 '해결 힌트(hint)'를 가지며,
각 예외는 프로세스 종료 코드(exit_code)를 정의한다.
스택트레이스 대신 이 두 문장을 사용자에게 보여준다.
"""

from __future__ import annotations


class BudgetError(Exception):
    """가계부 앱의 모든 예외의 기반 클래스."""

    exit_code: int = 1

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message: str = message
        self.hint: str = hint


class ValidationError(BudgetError):
    """입력값 검증 실패(날짜/금액/타입 형식 오류 등)."""

    exit_code = 2


class NotFoundError(BudgetError):
    """존재하지 않는 데이터 접근(없는 거래 id 등)."""

    exit_code = 3


class CategoryError(BudgetError):
    """카테고리 관련 오류(미등록/중복/사용 중 삭제 등)."""

    exit_code = 4


class StorageError(BudgetError):
    """파일 읽기/쓰기 중 발생한 저장소 오류."""

    exit_code = 5


class ImportExportError(BudgetError):
    """CSV 가져오기/내보내기 오류."""

    exit_code = 6
