"""진입점 — `python -m budget_app <command> [options]`.

argparse 로 인자를 파싱하고 해당 명령 핸들러로 분기한다.
로그는 stderr로 보내며, --verbose 를 주면 DEBUG 레벨까지 출력한다.
정상 종료는 0, 오류 종료는 0이 아닌 값(각 예외의 exit_code).
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from typing import Iterator

from .cli import build_parser
from .output import GuardedOutput, OutputError


def _silence_output(stream_name: str) -> None:
    """실패한 표준 출력의 종료 시 재flush를 막는다."""
    devnull_fd = None
    stream_fd = None
    try:
        stream_fd = getattr(sys, stream_name).fileno()
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        if devnull_fd != stream_fd:
            os.dup2(devnull_fd, stream_fd)
    except (AttributeError, OSError, ValueError):
        # 닫힌 fd나 fileno가 없는 대체 스트림도 안전하게 종료한다.
        setattr(sys, stream_name, None)
    finally:
        # 원래 fd가 닫혀 있었으면 os.open이 바로 그 번호를 재사용할 수 있다.
        if devnull_fd is not None and devnull_fd != stream_fd:
            try:
                os.close(devnull_fd)
            except OSError:
                pass


def _report_output_error(exc: OutputError) -> None:
    """작동하는 반대 채널에 인코딩 가능한 진단을 한 번만 출력한다."""
    target_name = "stderr" if exc.stream_name == "stdout" else "stdout"
    target = getattr(sys, target_name)
    if target is None:
        return
    english = (
        f"[error] Cannot write {exc.stream_name}: {exc.cause}\n"
        "[hint] Check the output connection, disk space and write limit; "
        "use UTF-8 for text output (PYTHONIOENCODING=utf-8).\n"
    ).encode("ascii", errors="backslashreplace").decode("ascii")
    message = (
        f"[오류] 출력 내용을 쓰지 못했습니다 ({exc.stream_name}): {exc.cause}\n"
        "[힌트] 출력 파일의 여유 공간·쓰기 한도와 출력 연결 상태를 확인해 주세요.\n"
    )
    if isinstance(exc.cause, UnicodeError):
        message = english
    else:
        try:
            message.encode(getattr(target, "encoding", None) or "utf-8")
        except (UnicodeError, LookupError):
            message = english
    try:
        target.write(message)
        target.flush()
    except (OSError, UnicodeError, ValueError):
        _silence_output(target_name)


@contextmanager
def _guard_outputs() -> Iterator[None]:
    """인자 파싱부터 종료 전 양쪽 flush까지 같은 출력 오류 정책을 적용한다."""
    originals = {name: getattr(sys, name) for name in ("stdout", "stderr")}
    guarded = {name: GuardedOutput(name, stream) for name, stream in originals.items()
               if stream is not None}
    failure = None
    try:
        for name, stream in guarded.items():
            setattr(sys, name, stream)
        try:
            yield
        except OutputError as exc:
            failure = exc
        except BrokenPipeError as exc:
            # print를 대체한 호출자 등에서도 기존 파이프 중단 정책을 유지한다.
            failure = OutputError("stdout", exc)
    finally:
        failed_names = set()
        for name, stream in guarded.items():
            try:
                stream.flush()
            except OutputError as exc:
                failed_names.add(name)
                if failure is None:
                    failure = exc
        for name, stream in originals.items():
            setattr(sys, name, stream)
        if failure is not None:
            failed_names.add(failure.stream_name)
            for name in failed_names:
                _silence_output(name)
            # 이미 실패한 반대 채널에는 진단도 쓰지 않는다.
            if (len(failed_names) == 1
                    and not (failure.stream_name == "stdout"
                             and isinstance(failure.cause, BrokenPipeError))):
                _report_output_error(failure)
            raise failure


class _CommandLogHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # 기본 핸들러는 출력 예외를 삼키고 traceback을 stderr에 쓴다.
        # 명령의 출력 보호 경계가 오류와 종료 코드를 결정하도록 전달한다.
        raise


@contextmanager
def _command_logging(verbose: bool) -> Iterator[None]:
    logger = logging.getLogger("budget_app")
    saved = logger.level, logger.handlers[:], logger.propagate
    handler = _CommandLogHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[log] %(message)s"))
    try:
        logger.setLevel(logging.DEBUG if verbose else logging.WARNING)
        logger.handlers[:] = [handler]
        logger.propagate = False
        yield
    finally:
        logger.setLevel(saved[0])
        logger.handlers[:] = saved[1]
        logger.propagate = saved[2]
        handler.close()
        # 실패 traceback이 핸들러를 참조해도 logging.shutdown이 재flush하지 않게 한다.
        handler.stream = None


def main(argv: "list[str] | None" = None) -> int:
    try:
        with _guard_outputs():
            parser = build_parser()
            args = parser.parse_args(argv)
            with _command_logging(getattr(args, "verbose", False)):
                func = getattr(args, "func", None)
                if func is None:
                    parser.print_help()
                    return 1
                return func(args)
    except OutputError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
