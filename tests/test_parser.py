from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path

import pytest

from moneymanager_parser import MoneyManagerBackup
from moneymanager_parser.cli import main
from moneymanager_parser.core import (
    _from_epoch,
    _is_deleted,
    _iso_week_start,
    _kind,
    _month_label,
    _parse_date,
    _to_float,
)


def test_open_zip_raw_and_bytes(sample_mmbak: Path, raw_sqlite: Path) -> None:
    with MoneyManagerBackup.from_file(sample_mmbak) as backup:
        assert len(backup.transactions()) == 7
    with MoneyManagerBackup.from_file(raw_sqlite) as backup:
        assert backup.transactions()[0].category == "Food"
    data = sample_mmbak.read_bytes()
    with MoneyManagerBackup.from_bytes(data) as backup:
        assert backup.accounts()[0].name == "Cash"


def test_date_parsing_variants() -> None:
    assert _parse_date("2026-01-02") == date(2026, 1, 2)
    assert _parse_date("20260102") == date(2026, 1, 2)
    assert _parse_date("2026/01/02 00:00:00") == date(2026, 1, 2)
    assert _parse_date(int(datetime(2026, 1, 2).timestamp())) == date(2026, 1, 2)
    assert _parse_date(int(datetime(2026, 1, 2).timestamp() * 1000)) == date(2026, 1, 2)
    assert _parse_date("bad") is None


def test_type_mapping_and_transfer_exclusion(sample_mmbak: Path) -> None:
    with MoneyManagerBackup.from_file(sample_mmbak) as backup:
        kinds = [txn.kind for txn in backup.transactions()]
        assert kinds.count("transfer") == 1
        assert backup.query(kind="all").summary["count"] == 6
    with MoneyManagerBackup.from_file(
        sample_mmbak, type_map={0: "income", 1: "expense", 3: "transfer", 7: "transfer"}
    ) as backup:
        assert backup.query(kind="all").summary["count"] == 5
    assert _kind("unknown", {0: "income"}) == "expense"
    assert _to_float("12.5") == 12.5
    assert _to_float(None) == 0.0


def test_category_resolution_and_alias_schema(sample_mmbak: Path, alternate_mmbak: Path) -> None:
    with MoneyManagerBackup.from_file(sample_mmbak) as backup:
        txns = backup.transactions()
        assert {txn.category for txn in txns} >= {"Food", "Transit"}
        assert txns[0].account == "Cash"
        assert "Food" in backup.categories()
    with MoneyManagerBackup.from_file(alternate_mmbak) as backup:
        txn = backup.transactions()[0]
        assert txn.category == "Food"
        assert txn.account == "Wallet"
        assert backup.accounts()[0].balance == 42


def test_query_filters_groups_and_lists(sample_mmbak: Path) -> None:
    today = date.today()
    with MoneyManagerBackup.from_file(sample_mmbak) as backup:
        assert backup.query(month=today.strftime("%Y-%m"), kind="expense").summary["total"] == 200
        assert backup.query(category="food", kind="all").summary["count"] >= 2
        assert backup.query(search="coffee", kind="expense").summary["total"] == 120
        assert backup.query(kind="income").summary["count"] == 2
        assert backup.query(group_by="month", kind="all").groups
        assert backup.query(group_by="category", kind="expense").groups[0]["key"] in {
            "Food",
            "Transit",
        }
        assert len(backup.query(top=2).top) == 2
        assert len(backup.query(list_n=3).transactions) == 3
        assert (
            backup.query(
                date_from=today.isoformat(), date_to=today.isoformat(), kind="all"
            ).summary["count"]
            == 3
        )
        with pytest.raises(ValueError):
            backup.query(kind="bad")  # type: ignore[arg-type]


def test_schema_and_helpers(sample_mmbak: Path) -> None:
    with MoneyManagerBackup.from_file(sample_mmbak) as backup:
        schema = backup.schema()
        assert schema["transaction_table"] == "INOUTCOME"
        assert schema["resolved_columns"]["amount"] == "ZMONEY"
        assert schema["do_type_breakdown"]
    assert _month_label(date(2026, 2, 3)) == "2026-02"
    assert _iso_week_start(date(2026, 2, 4)).weekday() == 0


def test_currency_reading(sample_mmbak: Path) -> None:
    with MoneyManagerBackup.from_file(sample_mmbak) as backup:
        currencies = backup.currencies()
        assert {c.iso for c in currencies} == {"INR", "USD"}
        main = backup.currency()
        assert main is not None
        assert main.iso == "USD"
        assert main.symbol == "$"
        assert main.is_main is True


def test_currency_absent_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "nocur.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE INOUTCOME (ZMONEY TEXT, WDATE TEXT, DO_TYPE TEXT)")
    con.execute("INSERT INTO INOUTCOME VALUES ('10', '2026-01-01', '1')")
    con.commit()
    con.close()
    with MoneyManagerBackup.from_file(db) as backup:
        assert backup.currencies() == []
        assert backup.currency() is None


def test_currency_falls_back_to_first_when_no_main(tmp_path: Path) -> None:
    db = tmp_path / "altcur.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE INOUTCOME (amount TEXT, date TEXT, type TEXT);
        CREATE TABLE currency (id TEXT, code TEXT, symbol TEXT, name TEXT);
        """
    )
    con.execute("INSERT INTO INOUTCOME VALUES ('10', '2026-01-01', '1')")
    con.execute("INSERT INTO currency VALUES ('1', 'EUR', '€', 'Euro')")
    con.commit()
    con.close()
    with MoneyManagerBackup.from_file(db) as backup:
        main = backup.currency()
        assert main is not None
        assert main.iso == "EUR"
        assert main.symbol == "€"
        assert main.is_main is False


def test_cli_smoke(sample_mmbak: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["schema", str(sample_mmbak)]) == 0
    assert json.loads(capsys.readouterr().out)["transaction_table"] == "INOUTCOME"
    assert main(["currency", str(sample_mmbak)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["main"]["iso"] == "USD"
    assert len(payload["all"]) == 2
    assert main(["query", str(sample_mmbak), "--top", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["top"]
    assert main(["schema", str(sample_mmbak.with_name("missing.mmbak"))]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_invalid_inputs(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mmbak"
    with zipfile.ZipFile(empty, "w"):
        pass
    with pytest.raises(ValueError):
        MoneyManagerBackup.from_file(empty)
    db = tmp_path / "bad.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE something (value TEXT)")
    con.commit()
    con.close()
    with pytest.raises(ValueError):
        MoneyManagerBackup.from_file(db)


def test_query_default_categories_and_more_groups(sample_mmbak: Path) -> None:
    with MoneyManagerBackup.from_file(sample_mmbak) as backup:
        default = backup.query().as_dict()
        assert default["categories"]
        assert backup.query(group_by="day").as_dict()["groups"]
        assert backup.query(group_by="week").as_dict()["groups"]
        empty = backup.query(category="missing").as_dict()
        assert empty["summary"]["count"] == 0
        assert "categories" not in empty


def test_raw_from_bytes_and_missing_optional_tables(tmp_path: Path) -> None:
    db = tmp_path / "minimal.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE INOUTCOME (ZMONEY TEXT, WDATE TEXT, DO_TYPE TEXT)")
    con.execute("INSERT INTO INOUTCOME VALUES ('10', '2026-01-01', 'income')")
    con.commit()
    con.close()
    with MoneyManagerBackup.from_bytes(db.read_bytes()) as backup:
        txn = backup.transactions()[0]
        assert txn.kind == "income"
        assert txn.category == "Uncategorized"
        assert backup.accounts() == []
        assert backup.categories() == ["Uncategorized"]
        assert "do_type_breakdown" in backup.schema()


def test_private_temp_connection_fallback(tmp_path: Path) -> None:
    from moneymanager_parser.core import _temp_connection_from_bytes

    db = tmp_path / "fallback.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE demo (value TEXT)")
    con.execute("INSERT INTO demo VALUES ('ok')")
    con.commit()
    con.close()
    backup = _temp_connection_from_bytes(db.read_bytes(), tmp_path)
    rows = backup.execute("SELECT value FROM demo").fetchall()
    temp_name = Path(backup.execute("PRAGMA database_list").fetchone()[2])
    assert rows[0][0] == "ok"
    assert temp_name.exists()
    backup.close()
    assert not temp_name.exists()


def test_more_date_and_query_branches(tmp_path: Path) -> None:
    assert _parse_date(123) is None
    assert _parse_date("2026-99-99") is None
    db = tmp_path / "future.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE INOUTCOME (ZMONEY TEXT, WDATE TEXT, DO_TYPE TEXT, ZCONTENT TEXT)")
    con.execute("INSERT INTO INOUTCOME VALUES ('10', '2999-01-01', '1', 'future')")
    con.commit()
    con.close()
    with MoneyManagerBackup.from_file(db) as backup:
        assert backup.query(month="2999-01").as_dict()["summary"]["total"] == 10
        assert backup.query(search="none").as_dict()["summary"]["total"] == 0


def test_realistic_schema_names_balances_and_deletes(realistic_mmbak: Path) -> None:
    with MoneyManagerBackup.from_file(realistic_mmbak) as backup:
        txns = backup.transactions()
        assert len(txns) == 3
        assert {t.account for t in txns} == {"Checking"}
        transfer = next(t for t in txns if t.kind == "transfer")
        assert transfer.account == "Checking"
        assert transfer.to_account == "Savings"

        balances = {a.name: a.balance for a in backup.accounts()}
        assert balances == {"Checking": 2400.0, "Savings": 500.0}

        assert "GoneCategory" not in backup.categories()

        currencies = backup.currencies()
        assert {c.iso for c in currencies} == {"USD"}
        main = backup.currency()
        assert main is not None and main.iso == "USD"


def test_transactions_are_memoized(sample_mmbak: Path) -> None:
    with MoneyManagerBackup.from_file(sample_mmbak) as backup:
        first = backup.transactions()
        second = backup.transactions()
        assert first == second
        assert first is not second  # returns a fresh list, not the cache


def test_connection_is_read_only(sample_mmbak: Path) -> None:
    with MoneyManagerBackup.from_file(sample_mmbak) as backup:
        with pytest.raises(sqlite3.OperationalError):
            backup._con.execute("CREATE TABLE hack (x TEXT)")


def test_is_deleted_variants() -> None:
    assert _is_deleted(1) is True
    assert _is_deleted(-1) is True
    assert _is_deleted("yes") is True
    assert _is_deleted("true") is True
    assert _is_deleted(0) is False
    assert _is_deleted(None) is False
    assert _is_deleted("") is False
    assert _is_deleted("no") is False
    assert _is_deleted("nonsense") is False


def test_from_epoch_bounds() -> None:
    assert _from_epoch(123) is None
    assert _from_epoch(99999999999999999) is None  # year well past 2100
    assert _parse_date("99999999999999999") is None


def test_zip_bomb_guard(realistic_mmbak: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import moneymanager_parser.core as core

    monkeypatch.setattr(core, "MAX_DB_BYTES", 16)
    with pytest.raises(ValueError, match="oversized"):
        MoneyManagerBackup.from_file(realistic_mmbak)
