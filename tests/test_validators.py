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
    def test_iso_date_preserves_leap_days_and_four_digit_year_boundaries(self) -> None:
        for value, expected in (("0001-1-5", "0001-01-05"), ("9999-12-31", "9999-12-31"),
                                ("2000-2-29", "2000-02-29"), (" 2024-2-29 ", "2024-02-29")):
            with self.subTest(value=value):
                self.assertEqual(validate_date(value), expected)
        for value in ("1900-02-29", "2023-02-29", "0000-01-01", "10000-01-01"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_date(value)

    def test_normalizes_non_zero_padded(self) -> None:
        # strptime 은 통과하지만 저장은 정규화돼야 한다(회귀 방지).
        self.assertEqual(validate_date("2024-1-5"), "2024-01-05")
        self.assertEqual(validate_date(" 2024-01-05 "), "2024-01-05")

    def test_rejects_invalid(self) -> None:
        for bad in ("2024-13-40", "2024-02-30", "2024/01/01", "", "abc"):
            with self.assertRaises(ValidationError):
                validate_date(bad)

    def test_fast_and_fallback_paths_have_same_calendar_validation(self) -> None:
        for value in ("0001-01-01", "2000-02-29", "2024-04-30", "9999-12-31"):
            year, month, day = value.split("-")
            with self.subTest(value=value):
                self.assertEqual(validate_date(value), value)
                self.assertEqual(validate_date(f"{year}-{int(month)}-{int(day)}"), value)
        for value in ("1900-02-29", "2023-02-29", "2024-04-31", "2024-00-10", "2024-01-00"):
            year, month, day = value.split("-")
            for spelling in (value, f"{year}-{int(month)}-{int(day)}"):
                with self.subTest(value=spelling), self.assertRaises(ValidationError):
                    validate_date(spelling)

    def test_does_not_accept_other_iso_formats(self) -> None:
        for value in ("20240105", "2024-W01-5", "2024W015", "2024-01-05T00:00:00", "2024-005"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_date(value)


class ValidateMonthTest(unittest.TestCase):
    def test_four_digit_year_boundaries(self) -> None:
        self.assertEqual(validate_month("0001-1"), "0001-01")
        self.assertEqual(validate_month("9999-12"), "9999-12")

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
