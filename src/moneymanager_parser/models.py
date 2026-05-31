"""Public dataclasses returned by the SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

Kind = Literal["income", "expense", "transfer"]
GroupBy = Literal["month", "category", "day", "week"]
QueryKind = Literal["expense", "income", "all"]

JsonValue = Any
JsonDict = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Transaction:
    amount: float
    date: date
    kind: Kind
    category: str
    memo: str
    account: str | None = None


@dataclass(frozen=True, slots=True)
class Account:
    name: str
    balance: float


@dataclass(frozen=True, slots=True)
class Currency:
    iso: str
    symbol: str
    name: str = ""
    is_main: bool = False


@dataclass(frozen=True, slots=True)
class QueryResult:
    status: str
    filters: JsonDict
    summary: JsonDict
    group_by: str | None = None
    groups: list[JsonDict] = field(default_factory=list)
    top: list[JsonDict] = field(default_factory=list)
    transactions: list[JsonDict] = field(default_factory=list)
    categories: list[JsonDict] = field(default_factory=list)

    def as_dict(self) -> JsonDict:
        out: JsonDict = {"status": self.status, "filters": self.filters, "summary": self.summary}
        if self.group_by:
            out["group_by"] = self.group_by
            out["groups"] = self.groups
        if self.top:
            out["top"] = self.top
        if self.transactions:
            out["transactions"] = self.transactions
        if self.categories:
            out["categories"] = self.categories
        return out
