"""검증 계층 테스트 (validators)."""

from __future__ import annotations

import unittest

from budget_app.errors import ValidationError
from budget_app.validators import (
    parse_tags,
    validate_amount,
    validate_date,
    validate_month,
    validate_type,
)


class ValidateDateTest(unittest.TestCase):
    def test_normalizes_non_zero_padded(self) -> None:
        # strptime 은 통과하지만 저장은 정규화돼야 한다(회귀 방지).
        self.assertEqual(validate_date("2024-1-5"), "2024-01-05")
        self.assertEqual(validate_date(" 2024-01-05 "), "2024-01-05")

    def test_rejects_invalid(self) -> None:
        for bad in ("2024-13-40", "2024-02-30", "2024/01/01", "", "abc"):
            with self.assertRaises(ValidationError):
                validate_date(bad)


class ValidateMonthTest(unittest.TestCase):
    def test_normalizes(self) -> None:
        self.assertEqual(validate_month("2024-1"), "2024-01")

    def test_rejects_invalid(self) -> None:
        for bad in ("2024-13", "2024", "24-01", ""):
            with self.assertRaises(ValidationError):
                validate_month(bad)


class ValidateAmountTest(unittest.TestCase):
    def test_accepts_positive_int(self) -> None:
        self.assertEqual(validate_amount("15000"), 15000)
        self.assertEqual(validate_amount(15000), 15000)

    def test_rejects_non_positive_and_non_int(self) -> None:
        for bad in ("0", "-5", "15000.0", "abc", ""):
            with self.assertRaises(ValidationError):
                validate_amount(bad)


class ValidateTypeTest(unittest.TestCase):
    def test_accepts_and_lowercases(self) -> None:
        self.assertEqual(validate_type("INCOME"), "income")
        self.assertEqual(validate_type("expense"), "expense")

    def test_rejects_unknown(self) -> None:
        with self.assertRaises(ValidationError):
            validate_type("spend")


class ParseTagsTest(unittest.TestCase):
    def test_splits_and_strips(self) -> None:
        self.assertEqual(parse_tags("a, b , ,c"), ["a", "b", "c"])

    def test_empty(self) -> None:
        self.assertEqual(parse_tags(""), [])


if __name__ == "__main__":
    unittest.main()
