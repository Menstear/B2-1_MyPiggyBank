"""표준 출력에서 발생한 장애를 업무 처리 중의 예외와 구분한다."""

from __future__ import annotations

from typing import Any, TextIO


class OutputError(Exception):
    """stdout 또는 stderr의 실제 write/flush 실패."""

    def __init__(self, stream_name: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.stream_name = stream_name
        self.cause = cause


class GuardedOutput:
    """출력 인터페이스는 유지하고, 실패한 채널에 다시 쓰는 것을 막는다."""

    def __init__(self, stream_name: str, stream: TextIO) -> None:
        self.stream_name = stream_name
        self.stream = stream
        self.failure: OutputError | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)

    def write(self, text: str) -> int:
        if self.failure is not None:
            raise self.failure
        try:
            return self.stream.write(text)
        except (OSError, UnicodeError, ValueError) as exc:
            self.failure = OutputError(self.stream_name, exc)
            raise self.failure from exc

    def flush(self) -> None:
        if self.failure is not None:
            raise self.failure
        try:
            self.stream.flush()
        except (OSError, UnicodeError, ValueError) as exc:
            self.failure = OutputError(self.stream_name, exc)
            raise self.failure from exc
