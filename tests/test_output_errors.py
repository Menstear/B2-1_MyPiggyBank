"""표준 출력 쓰기/flush 장애와 실제 OS 파일 쓰기 한도 회귀 테스트."""

from __future__ import annotations

import errno
import io
import logging
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from budget_app.__main__ import main
from budget_app.cli import build_service
from budget_app.models import Transaction

try:
    import resource
except ImportError:  # Windows 등 resource 모듈이 없는 환경
    resource = None

ROOT = Path(__file__).resolve().parents[1]
HAS_FILE_LIMIT = (resource is not None and hasattr(resource, "RLIMIT_FSIZE")
                  and hasattr(signal, "SIGXFSZ"))


class FailingOutput(io.StringIO):
    def __init__(self, stage: str, fd: int | None = None) -> None:
        super().__init__()
        self.stage = stage
        self.fd = fd

    def write(self, value: str) -> int:
        if self.stage == "write":
            raise OSError(errno.ENOSPC, "No space left on device")
        return super().write(value)

    def flush(self) -> None:
        if self.stage == "flush":
            raise OSError(errno.ENOSPC, "No space left on device")
        super().flush()

    def fileno(self) -> int:
        return super().fileno() if self.fd is None else self.fd


class TransientFailingOutput(io.StringIO):
    """첫 write만 실패해서 logging이 오류를 삼키는 회귀를 드러낸다."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def write(self, value: str) -> int:
        self.attempts += 1
        if self.attempts == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return super().write(value)


class OutputErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.data_dir = self.base / "data"
        # 파일 크기 제한을 걸기 전에 초기화를 끝낸다. 실패 대상은 출력 파일뿐이다.
        build_service(str(self.data_dir))

    @unittest.skipUnless(HAS_FILE_LIMIT, "OS 파일 쓰기 한도를 지원하지 않음")
    def test_real_file_limit_reports_cause_and_hint_without_shutdown_traceback(self) -> None:
        script = (
            "import resource, signal, sys; from budget_app.__main__ import main; "
            "signal.signal(signal.SIGXFSZ, signal.SIG_IGN); "
            "resource.setrlimit(resource.RLIMIT_FSIZE, (1, 1)); "
            "sys.exit(main(sys.argv[1:]))"
        )
        for flags in ([], ["-u"]):
            with self.subTest(flags=flags), (self.base / "stdout.txt").open("wb") as output:
                result = subprocess.run(
                    [sys.executable, *flags, "-c", script, "list", "--data-dir", str(self.data_dir)],
                    cwd=ROOT, stdout=output, stderr=subprocess.PIPE, text=True, timeout=10,
                )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("[오류] 출력 내용을 쓰지 못했습니다", result.stderr)
            self.assertIn("[힌트]", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("Exception ignored", result.stderr)
            self.assertEqual((self.base / "stdout.txt").stat().st_size, 1)

    @unittest.skipUnless(HAS_FILE_LIMIT, "OS 파일 쓰기 한도를 지원하지 않음")
    def test_failure_of_both_output_streams_still_exits_with_managed_code(self) -> None:
        script = (
            "import resource, signal, sys; from budget_app.__main__ import main; "
            "signal.signal(signal.SIGXFSZ, signal.SIG_IGN); "
            "resource.setrlimit(resource.RLIMIT_FSIZE, (1, 1)); "
            "sys.exit(main(sys.argv[1:]))"
        )
        for flags in ([], ["-u"]):
            with self.subTest(flags=flags), (self.base / "stdout.txt").open("wb") as output, \
                    (self.base / "stderr.txt").open("wb") as errors:
                result = subprocess.run(
                    [sys.executable, *flags, "-c", script, "list", "--data-dir", str(self.data_dir)],
                    cwd=ROOT, stdout=output, stderr=errors, timeout=10,
                )
            self.assertEqual(result.returncode, 1)
            self.assertEqual((self.base / "stderr.txt").stat().st_size, 1)

    def test_closed_stdout_descriptor_is_recovered_without_closing_its_replacement(self) -> None:
        script = (
            "import os, sys; from budget_app.__main__ import main; "
            "os.close(sys.stdout.fileno()); sys.exit(main(sys.argv[1:]))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, "list", "--data-dir", str(self.data_dir)],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("[오류]", result.stderr)
        self.assertIn("[힌트]", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Exception ignored", result.stderr)

    def test_closed_stderr_descriptor_is_reported_without_shutdown_warning(self) -> None:
        script = (
            "import os, sys; from budget_app.__main__ import main; "
            "os.close(sys.stderr.fileno()); sys.exit(main(sys.argv[1:]))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, "--verbose", "list", "--data-dir", str(self.data_dir)],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[오류]", result.stdout)
        self.assertIn("[힌트]", result.stdout)
        self.assertEqual(result.stderr, "")

    @unittest.skipUnless(HAS_FILE_LIMIT, "OS 파일 쓰기 한도를 지원하지 않음")
    def test_stderr_file_limit_is_reported_to_stdout_in_both_buffer_modes(self) -> None:
        script = (
            "import resource, signal, sys; from budget_app.__main__ import main; "
            "signal.signal(signal.SIGXFSZ, signal.SIG_IGN); "
            "resource.setrlimit(resource.RLIMIT_FSIZE, (1, 1)); "
            "sys.exit(main(sys.argv[1:]))"
        )
        for flags in ([], ["-u"]):
            with self.subTest(flags=flags), (self.base / "stderr.txt").open("wb") as errors:
                result = subprocess.run(
                    [sys.executable, *flags, "-c", script, "--verbose", "list",
                     "--data-dir", str(self.data_dir)],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=errors, text=True, timeout=10,
                )
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("[오류]", result.stdout)
            self.assertIn("stderr", result.stdout)
            self.assertIn("[힌트]", result.stdout)
            self.assertNotIn("Traceback", result.stdout)
            self.assertEqual((self.base / "stderr.txt").stat().st_size, 1)

    def test_transient_log_write_failure_is_not_swallowed_or_retried(self) -> None:
        output, errors = io.StringIO(), TransientFailingOutput()
        with patch("sys.stdout", output), patch("sys.stderr", errors):
            result = main(["--verbose", "list", "--data-dir", str(self.data_dir)])
            self.assertIsNone(sys.stderr)
        self.assertEqual(result, 1)
        self.assertEqual(errors.attempts, 1)
        self.assertEqual(errors.getvalue(), "")
        self.assertIn("No space left on device", output.getvalue())
        self.assertIn("[힌트]", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_stderr_final_flush_failure_is_detected_without_verbose_logging(self) -> None:
        output, errors = io.StringIO(), FailingOutput("flush")
        with patch("sys.stdout", output), patch("sys.stderr", errors):
            result = main(["list", "--data-dir", str(self.data_dir)])
            self.assertIsNone(sys.stderr)
        self.assertEqual(result, 1)
        self.assertIn("No space left on device", output.getvalue())
        self.assertIn("[힌트]", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

    def test_narrow_output_encodings_have_ascii_diagnostics_for_all_entry_paths(self) -> None:
        script = (
            "import sys; from budget_app.__main__ import main; "
            # Python의 기본 stderr는 backslashreplace이므로 strict 경로도 검증한다.
            "sys.stderr.reconfigure(errors='strict'); sys.exit(main(sys.argv[1:]))"
        )
        commands = (
            ["list", "--data-dir", str(self.data_dir)],
            ["--help"],
            ["--verbose", "list", "--data-dir", str(self.data_dir)],
            ["없는명령"],
        )
        for encoding in ("ascii", "cp1252"):
            for command in commands:
                with self.subTest(encoding=encoding, command=command):
                    env = dict(os.environ, PYTHONIOENCODING=encoding)
                    result = subprocess.run(
                        [sys.executable, "-c", script, *command],
                        cwd=ROOT, env=env, capture_output=True, timeout=10,
                    )
                    text = (result.stdout + result.stderr).decode("ascii")
                    self.assertEqual(result.returncode, 1, text)
                    self.assertIn("[error]", text)
                    self.assertIn("[hint]", text)
                    self.assertIn("UTF-8", text)
                    self.assertNotIn("Traceback", text)
                    self.assertNotIn("Exception ignored", text)
                    diagnostic = result.stdout if "--verbose" in command or command == ["없는명령"] else result.stderr
                    self.assertIn(b"[error]", diagnostic)

    def test_default_stderr_encoding_fallback_preserves_usage_error_exit_code(self) -> None:
        for encoding in ("ascii", "cp1252"):
            with self.subTest(encoding=encoding):
                result = subprocess.run(
                    [sys.executable, "-m", "budget_app", "없는명령"],
                    cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING=encoding),
                    capture_output=True, timeout=10,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(b"invalid choice", result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)

    def test_failed_log_handler_is_not_flushed_again_at_interpreter_shutdown(self) -> None:
        script = '''
import errno, io, sys
from budget_app.__main__ import main
class TransientErrorStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.first_write = True
    def write(self, text):
        if self.first_write:
            self.first_write = False
            raise OSError(errno.ENOSPC, "No space left on device")
        return super().write(text)
original_stderr = sys.stderr
sys.stderr = TransientErrorStream()
code = main(sys.argv[1:])
sys.stderr = original_stderr
sys.exit(code)
'''
        result = subprocess.run(
            [sys.executable, "-c", script, "--verbose", "list", "--data-dir", str(self.data_dir)],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[힌트]", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_argparse_output_errors_override_help_and_usage_exit_codes(self) -> None:
        for command, failing_name in ((["--help"], "stdout"), (["invalid-command"], "stderr")):
            with self.subTest(command=command):
                failing = FailingOutput("write")
                healthy = io.StringIO()
                other_name = "stderr" if failing_name == "stdout" else "stdout"
                with patch(f"sys.{failing_name}", failing), patch(f"sys.{other_name}", healthy):
                    result = main(command)
                self.assertEqual(result, 1)
                self.assertIn("[오류]", healthy.getvalue())
                self.assertIn("[힌트]", healthy.getvalue())
                self.assertNotIn("Traceback", healthy.getvalue())

    def test_logging_configuration_is_restored_and_repeated_calls_do_not_duplicate_logs(self) -> None:
        app_logger = logging.getLogger("budget_app")
        root_logger = logging.getLogger()
        app_state = app_logger.level, app_logger.handlers[:], app_logger.propagate
        root_state = root_logger.level, root_logger.handlers[:]
        for _ in range(2):
            output, errors = io.StringIO(), io.StringIO()
            with patch("sys.stdout", output), patch("sys.stderr", errors):
                result = main(["--verbose", "list", "--data-dir", str(self.data_dir)])
            self.assertEqual(result, 0)
            self.assertEqual(errors.getvalue().count("실행: cmd_list"), 1)
            self.assertEqual(errors.getvalue().count("cmd_list 실행 시간:"), 1)
            self.assertEqual((app_logger.level, app_logger.handlers, app_logger.propagate), app_state)
            self.assertEqual((root_logger.level, root_logger.handlers), root_state)

    def test_io_and_encoding_failures_still_close_the_search_iterator(self) -> None:
        for kind in ("io", "encoding"):
            with self.subTest(kind=kind):
                closed = []

                def transactions():
                    try:
                        yield Transaction("TX-000001", "expense", "2024-01-01", 1000, "food", "점심")
                        self.fail("출력 실패 뒤 다음 거래를 읽으면 안 됩니다")
                    finally:
                        closed.append(True)

                service = Mock()
                service.search.return_value = transactions()
                output = (FailingOutput("write") if kind == "io"
                          else io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict"))
                self.addCleanup(output.close)
                errors = io.StringIO()
                with patch("sys.stdout", output), patch("sys.stderr", errors), \
                        patch("budget_app.cli.build_service", return_value=service):
                    result = main(["search", "--data-dir", str(self.data_dir)])
                self.assertEqual(result, 1)
                self.assertEqual(closed, [True])
                self.assertNotIn("Traceback", errors.getvalue())

    def test_no_space_during_write_or_final_flush_is_reported_to_stderr(self) -> None:
        for stage in ("write", "flush"):
            with self.subTest(stage=stage):
                output = FailingOutput(stage)
                errors = io.StringIO()
                with patch("sys.stdout", output), patch("sys.stderr", errors):
                    result = main(["list", "--data-dir", str(self.data_dir)])
                    self.assertIsNone(sys.stdout)  # fileno 없는 스트림의 재flush도 차단
                self.assertEqual(result, 1)
                self.assertIn("No space left on device", errors.getvalue())
                self.assertIn("[힌트]", errors.getvalue())
                self.assertNotIn("[오류]", output.getvalue())

    def test_failed_devnull_open_cannot_leak_a_second_exception(self) -> None:
        output = FailingOutput("flush", fd=999)
        errors = io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", errors), \
                patch("budget_app.__main__.os.open", side_effect=OSError(errno.EMFILE, "Too many open files")):
            result = main(["list", "--data-dir", str(self.data_dir)])
            self.assertIsNone(sys.stdout)
        self.assertEqual(result, 1)
        self.assertIn("No space left on device", errors.getvalue())
        self.assertNotIn("Too many open files", errors.getvalue())

    def test_non_output_oserror_keeps_the_existing_command_error_policy(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.stdout", output), patch("sys.stderr", errors), \
                patch("budget_app.cli.build_service", side_effect=OSError(errno.EACCES, "storage denied")):
            with self.assertRaises(SystemExit) as failure:
                main(["list", "--data-dir", str(self.data_dir)])
        self.assertEqual(failure.exception.code, 1)
        self.assertIn("storage denied", output.getvalue())
        self.assertIn("[힌트]", output.getvalue())
        self.assertNotIn("출력 내용을", output.getvalue())
        self.assertEqual(errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
