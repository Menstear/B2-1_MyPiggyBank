"""직렬화 스키마와 변경 가능한 필드의 복사 계약을 검증한다."""

import unittest

from budget_app.models import Transaction


class TransactionSerializationTest(unittest.TestCase):
    def test_fixed_schema_roundtrip(self) -> None:
        record = {
            "id": "TX-000001", "type": "expense", "date": "2024-02-29",
            "amount": 8000, "category": "food", "memo": '한글, "따옴표"\n줄바꿈',
            "tags": ["meal", "cafe"],
        }
        tx = Transaction.from_dict(record)
        self.assertEqual(tx.to_dict(), record)
        self.assertEqual(Transaction.from_dict(tx.to_dict()), tx)

    def test_serialized_tags_are_an_independent_snapshot(self) -> None:
        tx = Transaction("TX-000001", "expense", "2024-01-01", 1000, "food", tags=["meal"])
        record = tx.to_dict()
        record["tags"].append("snapshot-only")
        self.assertEqual(tx.tags, ["meal"])
        tx.tags.append("model-only")
        self.assertEqual(record["tags"], ["meal", "snapshot-only"])


if __name__ == "__main__":
    unittest.main()
