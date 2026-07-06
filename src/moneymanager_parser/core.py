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
    Currency,
    GroupBy,
    JsonDict,
    Kind,
    QueryKind,
    QueryResult,
    Transaction,
)
from .schema import (
    AMOUNT_COLS,
    ASSET_NAME_COLS,
    ASSET_TABLE_CANDIDATES,
    ASSETFK_COLS,
    ASSETTOFK_COLS,
    BALANCE_COLS,
    CATEGORY_TABLE_CANDIDATES,
    CATFK_COLS,
    CATNAME_COLS,
    CURRENCY_ISO_COLS,
    CURRENCY_MAIN_COLS,
    CURRENCY_SYMBOL_COLS,
    CURRENCY_TABLE_CANDIDATES,
    DATE_COLS,
    DEFAULT_TYPE_MAP,
    IS_DEL_COLS,
    MEMO_COLS,
    NAME_COLS,
    STRING_TYPE_MAP,
    TXN_TABLE_CANDIDATES,
    TYPE_COLS,
    UID_COLS,
)

TypeMap = Mapping[int, Kind]

# Hard cap on a single SQLite member extracted from a .mmbak ZIP, to guard
# against decompression bombs in untrusted backups.
MAX_DB_BYTES = 512 * 1024 * 1024


def _q(identifier: str) -> str:
    """Quote a SQL identifier, escaping embedded double quotes."""
    return '"' + identifier.replace('"', '""') + '"'


def _is_deleted(value: object) -> bool:
    """Interpret a soft-delete flag from heterogeneous backup schemas."""
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"", "0", "false", "f", "no", "n"}:
        return False
    if text in {"1", "true", "t", "yes", "y"}:
        return True
    try:
        return float(text) != 0
    except ValueError:
        return False


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
    return [str(row[1]) for row in con.execute(f"PRAGMA table_info({_q(table)})")]


def _list_tables(con: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]


def _pick_table(
    _con: sqlite3.Connection, candidates: Iterable[str], tables: Iterable[str]
) -> str | None:
    return _first_present(candidates, tables)


def _from_epoch(number: float) -> date | None:
    if number >= 1e12:
        seconds = number / 1000
    elif number >= 1e9:
        seconds = number
    else:
        return None
    try:
        moment = datetime.fromtimestamp(seconds)
    except (OverflowError, OSError, ValueError):
        return None
    if 1970 <= moment.year <= 2100:
        return moment.date()
    return None


def _parse_date(value: object) -> date | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    if text.lstrip("-").isdigit():
        digits = text.lstrip("-")
        if len(digits) == 8:
            try:
                return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
            except ValueError:
                return None
        return _from_epoch(float(int(text)))
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
    if readonly:
        con.execute("PRAGMA query_only=ON")
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
        con.execute("PRAGMA query_only=ON")
        return con
    con.close()
    return _temp_connection_from_bytes(data, directory)


def _select_zip_member(archive: zipfile.ZipFile, label: str) -> str:
    members = [name for name in archive.namelist() if not name.endswith("/")]
    if not members:
        raise ValueError(f"empty .mmbak archive: {label}")
    member = next(
        (name for name in members if name.lower().endswith((".sqlite", ".db", ".mmbak"))),
        None,
    )
    if member is None:
        member = max(members, key=lambda name: archive.getinfo(name).file_size)
    size = archive.getinfo(member).file_size
    if size > MAX_DB_BYTES:
        raise ValueError(
            f"refusing to extract oversized backup member ({size} bytes > {MAX_DB_BYTES})"
        )
    return member


def _read_db_bytes_from_zip(data: bytes, label: str = "backup") -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(_select_zip_member(archive, label))


def _open_mmbak(path: os.PathLike[str] | str) -> sqlite3.Connection:
    file_path = Path(path)
    if zipfile.is_zipfile(file_path):
        with zipfile.ZipFile(file_path) as archive:
            data = archive.read(_select_zip_member(archive, str(file_path)))
        return _connection_from_bytes(data, file_path.parent)
    return _connect_file(file_path, readonly=True)


def _name_map(
    con: sqlite3.Connection, table: str | None, name_cols: Iterable[str] = NAME_COLS
) -> dict[str, str]:
    if not table:
        return {}
    cols = _table_columns(con, table)
    name_col = _first_present(name_cols, cols)
    if not name_col:
        return {}
    id_cols = [col for col in UID_COLS if col in cols]
    if not id_cols:
        return {}
    del_col = _first_present(IS_DEL_COLS, cols)
    selected = ", ".join(f"{_q(col)} AS k{index}" for index, col in enumerate(id_cols))
    del_select = f", {_q(del_col)} AS d" if del_col else ""
    out: dict[str, str] = {}
    query = f"SELECT {selected}, {_q(name_col)} AS n{del_select} FROM {_q(table)}"
    for row in con.execute(query):
        if del_col and _is_deleted(row["d"]):
            continue
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
    asset_to_fk: str | None
    memo: str | None
    txn_del: str | None
    category_table: str | None
    asset_table: str | None
    currency_table: str | None


class MoneyManagerBackup:
    """A parsed, offline Realbyte Money Manager backup.

    Income rows are preserved and exposed. The backup's currencies are read from
    the ``CURRENCY`` table when present; ``currency()`` returns the main currency.
    Soft-deleted rows (``IS_DEL``/``C_IS_DEL``) are excluded throughout.
    """

    def __init__(
        self, con: sqlite3.Connection, *, type_map: Mapping[int, str] | None = None
    ) -> None:
        self._con = con
        self._type_map = _normal_type_map(type_map)
        self._schema = self._resolve_schema()
        self._categories = _name_map(con, self._schema.category_table)
        self._account_names = _name_map(con, self._schema.asset_table, ASSET_NAME_COLS)
        self._txn_cache: tuple[Transaction, ...] | None = None
        self._accounts_cache: tuple[Account, ...] | None = None
        self._currencies_cache: tuple[Currency, ...] | None = None

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
            asset_to_fk=_first_present(ASSETTOFK_COLS, cols),
            memo=_first_present(MEMO_COLS, cols),
            txn_del=_first_present(IS_DEL_COLS, cols),
            category_table=_pick_table(self._con, CATEGORY_TABLE_CANDIDATES, tables),
            asset_table=_pick_table(self._con, ASSET_TABLE_CANDIDATES, tables),
            currency_table=_pick_table(self._con, CURRENCY_TABLE_CANDIDATES, tables),
        )

    def _iter_rows(self) -> Iterator[sqlite3.Row]:
        s = self._schema
        cols = [f"{_q(s.amount)} AS amt", f"{_q(s.date_col)} AS dt"]
        cols.append(f"{_q(s.type_col)} AS ty" if s.type_col else "NULL AS ty")
        cols.append(f"{_q(s.category_fk)} AS cat" if s.category_fk else "NULL AS cat")
        cols.append(f"{_q(s.category_name)} AS catname" if s.category_name else "NULL AS catname")
        cols.append(f"{_q(s.asset_fk)} AS asset" if s.asset_fk else "NULL AS asset")
        cols.append(f"{_q(s.asset_to_fk)} AS toasset" if s.asset_to_fk else "NULL AS toasset")
        cols.append(f"{_q(s.memo)} AS memo" if s.memo else "NULL AS memo")
        cols.append(f"{_q(s.txn_del)} AS isdel" if s.txn_del else "NULL AS isdel")
        yield from self._con.execute(f"SELECT {', '.join(cols)} FROM {_q(s.txn_table)}")

    def transactions(self) -> list[Transaction]:
        if self._txn_cache is not None:
            return list(self._txn_cache)
        out: list[Transaction] = []
        for row in self._iter_rows():
            if _is_deleted(row["isdel"]):
                continue
            parsed = _parse_date(row["dt"])
            if parsed is None:
                continue
            cat = row["catname"] or self._categories.get(str(row["cat"])) or "Uncategorized"
            account = (
                self._account_names.get(str(row["asset"])) if row["asset"] is not None else None
            )
            to_account = (
                self._account_names.get(str(row["toasset"])) if row["toasset"] is not None else None
            )
            out.append(
                Transaction(
                    amount=abs(_to_float(row["amt"])),
                    date=parsed,
                    kind=_kind(row["ty"], self._type_map),
                    category=str(cat),
                    memo=str(row["memo"] or ""),
                    account=account,
                    to_account=to_account,
                )
            )
        self._txn_cache = tuple(out)
        return list(self._txn_cache)

    def categories(self) -> list[str]:
        names = set(self._categories.values())
        names.update(txn.category for txn in self.transactions())
        return sorted(names)

    def _derived_balances(self) -> dict[str, float]:
        """Best-effort account balances summed from transactions.

        Income adds and expense subtracts on the owning account. Transfers move
        the amount from the source account to the destination (``toAssetUid``)
        and are only applied when a destination is present, so paired transfer
        rows are not double-counted. Amounts are raw numeric sums with no
        currency conversion, so totals across currencies are not meaningful.
        """
        balances: dict[str, float] = {}
        for txn in self.transactions():
            if txn.kind == "income" and txn.account is not None:
                balances[txn.account] = balances.get(txn.account, 0.0) + txn.amount
            elif txn.kind == "expense" and txn.account is not None:
                balances[txn.account] = balances.get(txn.account, 0.0) - txn.amount
            elif txn.kind == "transfer" and txn.to_account is not None:
                if txn.account is not None:
                    balances[txn.account] = balances.get(txn.account, 0.0) - txn.amount
                balances[txn.to_account] = balances.get(txn.to_account, 0.0) + txn.amount
        return balances

    def accounts(self) -> list[Account]:
        if self._accounts_cache is None:
            self._accounts_cache = tuple(self._build_accounts())
        return list(self._accounts_cache)

    def _build_accounts(self) -> list[Account]:
        table = self._schema.asset_table
        if not table:
            return []
        cols = _table_columns(self._con, table)
        name_col = _first_present(ASSET_NAME_COLS, cols)
        if not name_col:
            return []
        balance_col = _first_present(BALANCE_COLS, cols)
        del_col = _first_present(IS_DEL_COLS, cols)
        derived = None if balance_col else self._derived_balances()
        select = [f"{_q(name_col)} AS n"]
        select.append(f"{_q(balance_col)} AS b" if balance_col else "NULL AS b")
        select.append(f"{_q(del_col)} AS d" if del_col else "NULL AS d")
        out: list[Account] = []
        for row in self._con.execute(f"SELECT {', '.join(select)} FROM {_q(table)}"):
            if del_col and _is_deleted(row["d"]):
                continue
            name = str(row["n"])
            balance = _to_float(row["b"]) if balance_col else (derived or {}).get(name, 0.0)
            out.append(Account(name=name, balance=balance))
        return out

    def currencies(self) -> list[Currency]:
        if self._currencies_cache is None:
            self._currencies_cache = tuple(self._build_currencies())
        return list(self._currencies_cache)

    def _build_currencies(self) -> list[Currency]:
        table = self._schema.currency_table
        if not table:
            return []
        cols = _table_columns(self._con, table)
        iso_col = _first_present(CURRENCY_ISO_COLS, cols)
        symbol_col = _first_present(CURRENCY_SYMBOL_COLS, cols)
        if not iso_col and not symbol_col:
            return []
        name_col = _first_present(NAME_COLS, cols)
        main_col = _first_present(CURRENCY_MAIN_COLS, cols)
        del_col = _first_present(IS_DEL_COLS, cols)
        select = [
            f"{_q(iso_col)} AS iso" if iso_col else "'' AS iso",
            f"{_q(symbol_col)} AS sym" if symbol_col else "'' AS sym",
            f"{_q(name_col)} AS nm" if name_col else "'' AS nm",
            f"{_q(main_col)} AS mn" if main_col else "0 AS mn",
            f"{_q(del_col)} AS d" if del_col else "NULL AS d",
        ]
        out: list[Currency] = []
        for row in self._con.execute(f"SELECT {', '.join(select)} FROM {_q(table)}"):
            if del_col and _is_deleted(row["d"]):
                continue
            out.append(
                Currency(
                    iso=str(row["iso"] or ""),
                    symbol=str(row["sym"] or ""),
                    name=str(row["nm"] or ""),
                    is_main=bool(_to_float(row["mn"])),
                )
            )
        return out

    def currency(self) -> Currency | None:
        items = self.currencies()
        if not items:
            return None
        for item in items:
            if item.is_main:
                return item
        return items[0]

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
            "asset_to_fk": s.asset_to_fk,
            "memo": s.memo,
            "is_del": s.txn_del,
        }
        if s.type_col:
            rows = self._con.execute(
                f"SELECT {_q(s.type_col)} AS ty, COUNT(*) n, SUM(ABS({_q(s.amount)})) total "
                f"FROM {_q(s.txn_table)} GROUP BY {_q(s.type_col)}"
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

    def query(
        self,
        *,
        date_from: str | date | None = None,
        date_to: str | date | None = None,
        month: str | None = None,
        category: str | None = None,
        account: str | None = None,
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
        account_want = account.strip().lower() if account else None
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
            if account_want and (txn.account is None or txn.account.lower() != account_want):
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
            "account": account,
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
                elif group_by == "account":
                    key = txn.account or "Unspecified"
                else:
                    key = txn.category
                bucket = groups.setdefault(key, [0.0, 0.0])
                bucket[0] += txn.amount
                bucket[1] += 1
            items = list(groups.items())
            items.sort(key=(lambda item: item[1][0]), reverse=True) if group_by in {
                "category",
                "account",
            } else items.sort(key=lambda item: item[0])
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
                    "account": txn.account,
                    "to_account": txn.to_account,
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
                    "account": txn.account,
                    "to_account": txn.to_account,
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
