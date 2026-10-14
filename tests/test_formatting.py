"""큰 정수의 십진수 표시와 전역 입력 제한 보존을 검증한다."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from budget_app.formatting import format_integer

ROOT = Path(__file__).resolve().parents[1]


class IntegerFormattingTest(unittest.TestCase):
    def test_normal_integers_keep_exact_decimal_representation(self) -> None:
        for value in (0, 1, -1, 999_999_999, 1_000_000_000, -1_000_000_001):
            with self.subTest(value=value):
                self.assertEqual(format_integer(value), str(value))

    @unittest.skipUnless(hasattr(sys, "set_int_max_str_digits"), "Python 3.11+ 정수 자릿수 제한")
    def test_oversized_values_keep_sign_and_zero_padding_without_changing_limit(self) -> None:
        script = """
import sys
from budget_app.formatting import format_integer

sys.set_int_max_str_digits(640)
value = 12345 * 10 ** 5005 + 67890
expected = "12345" + "0" * 5000 + "67890"
for number, text in ((value, expected), (-value, "-" + expected)):
    try:
        str(number)
    except ValueError:
        pass
    else:
        raise AssertionError("This test must exercise the digit limit")
    assert format_integer(number) == text
assert format_integer(0) == "0"
assert sys.get_int_max_str_digits() == 640
"""
        proc = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
