"""데이터 모델 계층.

거래/카테고리/예산을 dataclass로 정의한다.
타입 힌트로 각 레코드의 '계약(contract)'을 명확히 하고,
to_dict / from_dict 로 JSON 직렬화 경계를 한곳에 모은다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Transaction:
    """거래 내역 한 건."""

    id: str                 # 유일 식별자. 예: "TX-000012"
    type: str               # "income" | "expense"
    date: str               # "YYYY-MM-DD"
    amount: int             # 양수 정수
    category: str           # 등록된 카테고리명
    memo: str = ""          # 선택
    tags: list[str] = field(default_factory=list)  # 선택

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transaction":
        return cls(
            id=str(data["id"]),
            type=str(data["type"]),
            date=str(data["date"]),
            amount=int(data["amount"]),
            category=str(data["category"]),
            memo=str(data.get("memo", "") or ""),
            tags=list(data.get("tags", []) or []),
        )


@dataclass
class Category:
    """등록된 카테고리."""

    name: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Category":
        return cls(name=str(data["name"]))


@dataclass
class Budget:
    """월별 예산."""

    month: str   # "YYYY-MM"
    amount: int  # 양수 정수

    def to_dict(self) -> dict[str, Any]:
        return {"month": self.month, "amount": self.amount}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Budget":
        return cls(month=str(data["month"]), amount=int(data["amount"]))
