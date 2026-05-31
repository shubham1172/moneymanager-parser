"""Core parser for Realbyte Money Manager backup files."""

from __future__ import annotations

import calendar
import io
import os
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from .models import (
    Account,
    GroupBy,
    JsonDict,
    Kind,
    QueryKind,
    QueryResult,
    Summary,
    Transaction,
)
from .schema import (
    AMOUNT_COLS,
    ASSET_TABLE_CANDIDATES,
    ASSETFK_COLS,
    BALANCE_COLS,
    CATEGORY_TABLE_CANDIDATES,
    CATFK_COLS,
    CATNAME_COLS,
    DATE_COLS,
    DEFAULT_TYPE_MAP,
    MEMO_COLS,
    NAME_COLS,
    STRING_TYPE_MAP,
    TXN_TABLE_CANDIDATES,
    TYPE_COLS,
    UID_COLS,
)

TypeMap = Mapping[int, Kind]


def _first_present(options: Iterable[str], available: Iterable[str]) -> str | None:
    aset = set(available)
    for option in options:
        if option in aset:
            return option
    lower = {item.lower(): item for item in available}
    for option in options:
        found = lower.get(option.lower())
        if found is not None:
            return found
    return None


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')]


def _list_tables(con: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]


def _pick_table(
    _con: sqlite3.Connection, candidates: Iterable[str], tables: Iterable[str]
) -> str | None:
    return _first_present(candidates, tables)


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e12:
            return datetime.fromtimestamp(number / 1000).date()
        if number > 1e9:
            return datetime.fromtimestamp(number).date()
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lstrip("-").isdigit():
        number = int(text)
        if len(text) >= 12:
            return datetime.fromtimestamp(number / 1000).date()
        if len(text) in {10, 11}:
            return datetime.fromtimestamp(number).date()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    digits = "".join(char for char in text if char.isdigit())
    if len(digits) >= 8:
        try:
            return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    return None


def _to_float(value: object) -> float:
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return 0.0


def _normal_type_map(type_map: Mapping[int, str] | None) -> dict[int, Kind]:
    source = type_map or DEFAULT_TYPE_MAP
    out: dict[int, Kind] = {}
    for key, value in source.items():
        if value not in {"income", "expense", "transfer"}:
            raise ValueError(f"unsupported transaction kind: {value}")
        out[int(key)] = cast(Kind, value)
    return out


def _kind(do_type: object, type_map: Mapping[int, Kind]) -> Kind:
    if do_type is None:
        return "expense"
    if isinstance(do_type, str):
        text = do_type.strip()
        if text.lstrip("-").isdigit():
            return type_map.get(int(text), "expense")
        return cast(Kind, STRING_TYPE_MAP.get(text.lower(), "expense"))
    try:
        return type_map.get(int(cast(Any, do_type)), "expense")
    except (TypeError, ValueError):
        return "expense"


def _month_label(day: date) -> str:
    return day.strftime("%Y-%m")


def _iso_week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


class _ManagedConnection(sqlite3.Connection):
    _cleanup_path: Path | None = None

    def close(self) -> None:
        cleanup = self._cleanup_path
        try:
            super().close()
        finally:
            if cleanup is not None:
                cleanup.unlink(missing_ok=True)


def _connect_file(path: Path, *, readonly: bool, cleanup: Path | None = None) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro" if readonly else str(path)
    con = sqlite3.connect(uri, uri=readonly, factory=_ManagedConnection)
    con.row_factory = sqlite3.Row
    con._cleanup_path = cleanup
    return con


def _temp_connection_from_bytes(data: bytes, directory: Path | None = None) -> sqlite3.Connection:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", dir=directory, delete=False) as handle:
        handle.write(data)
        name = Path(handle.name)
    return _connect_file(name, readonly=True, cleanup=name)


def _connection_from_bytes(data: bytes, directory: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    if hasattr(con, "deserialize"):
        con.deserialize(data)
        return con
    con.close()
    return _temp_connection_from_bytes(data, directory)


def _read_db_bytes_from_zip(data: bytes, label: str = "backup") -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [name for name in archive.namelist() if not name.endswith("/")]
        if not members:
            raise ValueError(f"empty .mmbak archive: {label}")
        member = next(
            (name for name in members if name.lower().endswith((".sqlite", ".db", ".mmbak"))),
            None,
        )
        if member is None:
            member = max(members, key=lambda name: archive.getinfo(name).file_size)
        return archive.read(member)


def _open_mmbak(path: os.PathLike[str] | str) -> sqlite3.Connection:
    file_path = Path(path)
    data = file_path.read_bytes()
    if zipfile.is_zipfile(file_path):
        return _connection_from_bytes(
            _read_db_bytes_from_zip(data, str(file_path)), file_path.parent
        )
    return _connect_file(file_path, readonly=True)


def _name_map(con: sqlite3.Connection, table: str | None) -> dict[str, str]:
    if not table:
        return {}
    cols = _table_columns(con, table)
    name_col = _first_present(NAME_COLS, cols)
    if not name_col:
        return {}
    id_cols = [col for col in UID_COLS if col in cols]
    if not id_cols:
        return {}
    selected = ", ".join(f'"{col}" AS k{index}' for index, col in enumerate(id_cols))
    out: dict[str, str] = {}
    for row in con.execute(f'SELECT {selected}, "{name_col}" AS n FROM "{table}"'):
        for index in range(len(id_cols)):
            value = row[f"k{index}"]
            if value is not None and value != "":
                out.setdefault(str(value), str(row["n"]))
    return out


def _period_bound(value: str, *, is_end: bool) -> date:
    text = value.strip()
    parts = text.split("-")
    if len(parts) == 2:
        year, month = int(parts[0]), int(parts[1])
        if is_end:
            return date(year, month, calendar.monthrange(year, month)[1])
        return date(year, month, 1)
    return datetime.strptime(text, "%Y-%m-%d").date()


@dataclass(frozen=True, slots=True)
class _ResolvedSchema:
    txn_table: str
    amount: str
    date_col: str
    type_col: str | None
    category_fk: str | None
    category_name: str | None
    asset_fk: str | None
    memo: str | None
    category_table: str | None
    asset_table: str | None


class MoneyManagerBackup:
    """A parsed, offline Realbyte Money Manager backup.

    Income rows are preserved and exposed. Expense-oriented summaries should treat
    income as informational because app exports can vary by installation.
    """

    def __init__(
        self, con: sqlite3.Connection, *, type_map: Mapping[int, str] | None = None
    ) -> None:
        self._con = con
        self._type_map = _normal_type_map(type_map)
        self._schema = self._resolve_schema()
        self._categories = _name_map(con, self._schema.category_table)
        self._account_names = _name_map(con, self._schema.asset_table)

    @classmethod
    def from_file(
        cls, path: os.PathLike[str] | str, *, type_map: Mapping[int, str] | None = None
    ) -> MoneyManagerBackup:
        return cls(_open_mmbak(path), type_map=type_map)

    @classmethod
    def from_bytes(
        cls, data: bytes, *, type_map: Mapping[int, str] | None = None
    ) -> MoneyManagerBackup:
        payload = (
            _read_db_bytes_from_zip(data, "bytes") if zipfile.is_zipfile(io.BytesIO(data)) else data
        )
        return cls(_connection_from_bytes(payload), type_map=type_map)

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> MoneyManagerBackup:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _resolve_schema(self) -> _ResolvedSchema:
        tables = _list_tables(self._con)
        txn_table = _pick_table(self._con, TXN_TABLE_CANDIDATES, tables)
        if not txn_table:
            raise ValueError(f"no transaction table found; tables={tables}")
        cols = _table_columns(self._con, txn_table)
        amount = _first_present(AMOUNT_COLS, cols)
        date_col = _first_present(DATE_COLS, cols)
        if not amount or not date_col:
            raise ValueError(f"missing amount/date columns in {txn_table}: {cols}")
        return _ResolvedSchema(
            txn_table=txn_table,
            amount=amount,
            date_col=date_col,
            type_col=_first_present(TYPE_COLS, cols),
            category_fk=_first_present(CATFK_COLS, cols),
            category_name=_first_present(CATNAME_COLS, cols),
            asset_fk=_first_present(ASSETFK_COLS, cols),
            memo=_first_present(MEMO_COLS, cols),
            category_table=_pick_table(self._con, CATEGORY_TABLE_CANDIDATES, tables),
            asset_table=_pick_table(self._con, ASSET_TABLE_CANDIDATES, tables),
        )

    def _iter_rows(self) -> Iterator[sqlite3.Row]:
        s = self._schema
        cols = [f'"{s.amount}" AS amt', f'"{s.date_col}" AS dt']
        cols.append(f'"{s.type_col}" AS ty' if s.type_col else "NULL AS ty")
        cols.append(f'"{s.category_fk}" AS cat' if s.category_fk else "NULL AS cat")
        cols.append(f'"{s.category_name}" AS catname' if s.category_name else "NULL AS catname")
        cols.append(f'"{s.asset_fk}" AS asset' if s.asset_fk else "NULL AS asset")
        cols.append(f'"{s.memo}" AS memo' if s.memo else "NULL AS memo")
        yield from self._con.execute(f'SELECT {", ".join(cols)} FROM "{s.txn_table}"')

    def transactions(self) -> list[Transaction]:
        out: list[Transaction] = []
        for row in self._iter_rows():
            parsed = _parse_date(row["dt"])
            if parsed is None:
                continue
            cat = row["catname"] or self._categories.get(str(row["cat"])) or "Uncategorized"
            account = (
                self._account_names.get(str(row["asset"])) if row["asset"] is not None else None
            )
            out.append(
                Transaction(
                    amount=abs(_to_float(row["amt"])),
                    date=parsed,
                    kind=_kind(row["ty"], self._type_map),
                    category=str(cat),
                    memo=str(row["memo"] or ""),
                    account=account,
                )
            )
        return out

    def categories(self) -> list[str]:
        names = set(self._categories.values())
        names.update(txn.category for txn in self.transactions())
        return sorted(names)

    def accounts(self) -> list[Account]:
        asset_table = self._schema.asset_table
        if not asset_table:
            return []
        cols = _table_columns(self._con, asset_table)
        name_col = _first_present(NAME_COLS, cols)
        balance_col = _first_present(BALANCE_COLS, cols)
        if not name_col or not balance_col:
            return []
        return [
            Account(name=str(row["n"]), balance=_to_float(row["b"]))
            for row in self._con.execute(
                f'SELECT "{name_col}" AS n, "{balance_col}" AS b FROM "{asset_table}"'
            )
        ]

    def schema(self) -> JsonDict:
        tables = _list_tables(self._con)
        out: JsonDict = {"tables": {table: _table_columns(self._con, table) for table in tables}}
        s = self._schema
        out["transaction_table"] = s.txn_table
        out["resolved_columns"] = {
            "amount": s.amount,
            "date": s.date_col,
            "type": s.type_col,
            "category_fk": s.category_fk,
            "category_name": s.category_name,
            "asset_fk": s.asset_fk,
            "memo": s.memo,
        }
        if s.type_col:
            rows = self._con.execute(
                f'SELECT "{s.type_col}" AS ty, COUNT(*) n, SUM(ABS("{s.amount}")) total '
                f'FROM "{s.txn_table}" GROUP BY "{s.type_col}"'
            )
            out["do_type_breakdown"] = [
                {
                    "do_type": str(row["ty"]),
                    "count": int(row["n"]),
                    "abs_total": _to_float(row["total"]),
                }
                for row in rows
            ]
        return out

    def summary(self) -> Summary:
        today = date.today()
        cur_month = _month_label(today)
        prev_month_day = today.replace(day=1) - timedelta(days=1)
        prev_month = _month_label(prev_month_day)
        week_start = _iso_week_start(today)
        prev_week_start = week_start - timedelta(days=7)
        month_exp = month_inc = pm_exp = pm_inc = today_exp = 0.0
        week_exp = week_inc = pw_exp = pw_inc = 0.0
        monthly: dict[str, list[float]] = {}
        weekly: dict[str, float] = {}
        cats_month: dict[str, float] = {}
        daily_month: dict[str, float] = {}
        top_exp: list[tuple[float, str, str, str]] = []
        n_txn = 0
        for txn in self.transactions():
            if txn.kind == "transfer":
                continue
            n_txn += 1
            label = _month_label(txn.date)
            bucket = monthly.setdefault(label, [0.0, 0.0])
            if txn.kind == "expense":
                bucket[0] += txn.amount
                weekly[_iso_week_start(txn.date).isoformat()] = (
                    weekly.get(_iso_week_start(txn.date).isoformat(), 0.0) + txn.amount
                )
            else:
                bucket[1] += txn.amount
            if label == cur_month:
                if txn.kind == "expense":
                    month_exp += txn.amount
                    cats_month[txn.category] = cats_month.get(txn.category, 0.0) + txn.amount
                    top_exp.append(
                        (txn.amount, txn.memo or txn.category, txn.category, txn.date.isoformat())
                    )
                    daily_month[txn.date.isoformat()] = (
                        daily_month.get(txn.date.isoformat(), 0.0) + txn.amount
                    )
                    if txn.date == today:
                        today_exp += txn.amount
                else:
                    month_inc += txn.amount
            elif label == prev_month:
                if txn.kind == "expense":
                    pm_exp += txn.amount
                else:
                    pm_inc += txn.amount
            if txn.date >= week_start:
                if txn.kind == "expense":
                    week_exp += txn.amount
                else:
                    week_inc += txn.amount
            elif txn.date >= prev_week_start:
                if txn.kind == "expense":
                    pw_exp += txn.amount
                else:
                    pw_inc += txn.amount
        anchor = today.replace(day=1)
        months: list[str] = []
        for i in range(11, -1, -1):
            year = anchor.year
            month = anchor.month - i
            while month <= 0:
                month += 12
                year -= 1
            months.append(f"{year:04d}-{month:02d}")
        series: list[JsonDict] = []
        for label in months:
            exp, inc = monthly.get(label, [0.0, 0.0])
            series.append({"label": label, "expense": round(exp), "income": round(inc)})
        wk_series = [
            round(weekly.get((week_start - timedelta(days=7 * i)).isoformat(), 0.0))
            for i in range(7, -1, -1)
        ]
        cats_sorted = sorted(cats_month.items(), key=lambda item: item[1], reverse=True)
        top_exp.sort(reverse=True)
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        day_of_month = today.day
        daily_series = [
            {
                "date": date(today.year, today.month, day).isoformat(),
                "amount": round(
                    daily_month.get(date(today.year, today.month, day).isoformat(), 0.0)
                ),
            }
            for day in range(1, day_of_month + 1)
        ]
        avg_daily = month_exp / day_of_month if day_of_month else 0.0
        projected = avg_daily * days_in_month
        complete = [item for item in series if item["label"] != cur_month]
        last3 = [int(item["expense"]) for item in complete[-3:]]
        trailing_avg = sum(last3) / len(last3) if last3 else 0.0
        mom_pct = ((projected - pm_exp) / pm_exp * 100.0) if pm_exp else None
        wow_pct = ((week_exp - pw_exp) / pw_exp * 100.0) if pw_exp else None
        top_cat = cats_sorted[0] if cats_sorted else None
        top_cat_share = (top_cat[1] / month_exp * 100.0) if top_cat and month_exp else 0.0

        def money(value: float) -> str:
            return f"₹{int(round(value)):,}"

        insights: list[str] = []
        if month_exp:
            insights.append(
                f"Spent {money(month_exp)} so far this month over {day_of_month} day{'s' if day_of_month != 1 else ''} (~{money(avg_daily)}/day)."
            )
            if mom_pct is not None:
                insights.append(
                    f"On pace for ~{money(projected)} by month-end — {'up' if mom_pct >= 0 else 'down'} {abs(mom_pct):.0f}% vs last month ({money(pm_exp)})."
                )
            elif trailing_avg:
                insights.append(f"On pace for ~{money(projected)} by month-end.")
            if trailing_avg:
                vs = (projected - trailing_avg) / trailing_avg * 100.0
                insights.append(
                    f"Projection is {abs(vs):.0f}% {'above' if vs >= 0 else 'below'} your 3-month average of {money(trailing_avg)}."
                )
            if top_cat:
                insights.append(
                    f"Biggest category: {top_cat[0]} {money(top_cat[1])} ({top_cat_share:.0f}% of spend)."
                )
            if top_exp:
                amount, memo, category, _day = top_exp[0]
                tail = f" ({memo})" if memo and memo != category else ""
                insights.append(f"Largest single expense: {money(amount)} on {category}{tail}.")
            if wow_pct is not None:
                insights.append(
                    f"This week {money(week_exp)} — {'up' if wow_pct >= 0 else 'down'} {abs(wow_pct):.0f}% vs last week ({money(pw_exp)})."
                )
        data: JsonDict = {
            "status": "ok",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "month": {
                "label": cur_month,
                "expense": round(month_exp),
                "income": round(month_inc),
                "net": round(month_inc - month_exp),
            },
            "prev_month": {"label": prev_month, "expense": round(pm_exp), "income": round(pm_inc)},
            "today_expense": round(today_exp),
            "week": {"expense": round(week_exp), "income": round(week_inc)},
            "prev_week": {"expense": round(pw_exp), "income": round(pw_inc)},
            "monthly_series": series,
            "weekly_series": ",".join(str(value) for value in wk_series),
            "daily_series": daily_series,
            "categories_month": [
                {"name": name, "amount": round(amount)} for name, amount in cats_sorted[:12]
            ],
            "top_expenses_month": [
                {"amount": round(amount), "memo": memo[:40], "category": category, "date": day}
                for amount, memo, category, day in top_exp[:5]
            ],
            "stats": {
                "days_in_month": days_in_month,
                "day_of_month": day_of_month,
                "avg_daily": round(avg_daily),
                "projected_month": round(projected),
                "trailing_avg": round(trailing_avg),
                "mom_pct": round(mom_pct) if mom_pct is not None else None,
                "wow_pct": round(wow_pct) if wow_pct is not None else None,
                "top_category": top_cat[0] if top_cat else None,
                "top_category_amount": round(top_cat[1]) if top_cat else 0,
                "top_category_share": round(top_cat_share),
            },
            "insights": insights,
            "accounts": [
                {"name": account.name, "balance": round(account.balance)}
                for account in self.accounts()[:12]
            ],
            "counts": {"transactions": n_txn},
        }
        return Summary(data)

    def query(
        self,
        *,
        date_from: str | date | None = None,
        date_to: str | date | None = None,
        month: str | None = None,
        category: str | None = None,
        search: str | None = None,
        kind: QueryKind = "expense",
        group_by: GroupBy | None = None,
        top: int | None = None,
        list_n: int | None = None,
        limit: int = 24,
    ) -> QueryResult:
        if kind not in {"expense", "income", "all"}:
            raise ValueError("kind must be expense, income, or all")
        if month:
            date_from = month
            date_to = month
        start = _period_bound(date_from, is_end=False) if isinstance(date_from, str) else date_from
        end = _period_bound(date_to, is_end=True) if isinstance(date_to, str) else date_to
        cat_want = category.strip().lower() if category else None
        search_want = search.strip().lower() if search else None
        matches: list[Transaction] = []
        for txn in self.transactions():
            if txn.kind == "transfer":
                continue
            if kind != "all" and txn.kind != kind:
                continue
            if start and txn.date < start:
                continue
            if end and txn.date > end:
                continue
            if cat_want and txn.category.lower() != cat_want:
                continue
            if (
                search_want
                and search_want not in txn.memo.lower()
                and search_want not in txn.category.lower()
            ):
                continue
            matches.append(txn)
        total = sum(txn.amount for txn in matches)
        dates = [txn.date for txn in matches]
        filters: JsonDict = {
            "from": start.isoformat() if start else None,
            "to": end.isoformat() if end else None,
            "category": category,
            "search": search,
            "kind": kind,
        }
        summary: JsonDict = {
            "total": round(total),
            "count": len(matches),
            "avg_per_txn": round(total / len(matches)) if matches else 0,
            "date_min": min(dates).isoformat() if dates else None,
            "date_max": max(dates).isoformat() if dates else None,
        }
        result = QueryResult(status="ok", filters=filters, summary=summary)
        if group_by:
            groups: dict[str, list[float]] = {}
            for txn in matches:
                if group_by == "month":
                    key = _month_label(txn.date)
                elif group_by == "day":
                    key = txn.date.isoformat()
                elif group_by == "week":
                    key = _iso_week_start(txn.date).isoformat()
                else:
                    key = txn.category
                bucket = groups.setdefault(key, [0.0, 0.0])
                bucket[0] += txn.amount
                bucket[1] += 1
            items = list(groups.items())
            items.sort(
                key=(lambda item: item[1][0]), reverse=True
            ) if group_by == "category" else items.sort(key=lambda item: item[0])
            result = QueryResult(
                status="ok",
                filters=filters,
                summary=summary,
                group_by=group_by,
                groups=[
                    {"key": key, "total": round(value[0]), "count": int(value[1])}
                    for key, value in items[:limit]
                ],
            )
        if top:
            tops = sorted(matches, key=lambda txn: txn.amount, reverse=True)[:top]
            result.top.extend(
                {
                    "amount": round(txn.amount),
                    "category": txn.category,
                    "memo": txn.memo[:60],
                    "date": txn.date.isoformat(),
                }
                for txn in tops
            )
        if list_n:
            recent = sorted(matches, key=lambda txn: txn.date, reverse=True)[:list_n]
            result.transactions.extend(
                {
                    "date": txn.date.isoformat(),
                    "amount": round(txn.amount),
                    "category": txn.category,
                    "memo": txn.memo[:60],
                }
                for txn in recent
            )
        if not (group_by or top or list_n):
            cats: dict[str, float] = {}
            for txn in matches:
                cats[txn.category] = cats.get(txn.category, 0.0) + txn.amount
            result.categories.extend(
                {"name": name, "total": round(value)}
                for name, value in sorted(cats.items(), key=lambda item: item[1], reverse=True)[
                    :limit
                ]
            )
        return result


def parse_mmbak(
    path: os.PathLike[str] | str, type_map: Mapping[int, str] | None = None
) -> JsonDict:
    with MoneyManagerBackup.from_file(path, type_map=type_map) as backup:
        return backup.summary().as_dict()
