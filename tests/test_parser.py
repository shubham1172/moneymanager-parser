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
    _iso_week_start,
    _kind,
    _month_label,
    _parse_date,
    _to_float,
    parse_mmbak,
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


def test_summary_schema_and_helpers(sample_mmbak: Path) -> None:
    with MoneyManagerBackup.from_file(sample_mmbak) as backup:
        summary = backup.summary().as_dict()
        assert summary["status"] == "ok"
        assert summary["month"]["expense"] == 200
        assert summary["week"]["expense"] >= 0
        assert summary["categories_month"]
        assert summary["top_expenses_month"]
        assert summary["accounts"]
        assert summary["counts"]["transactions"] == 6
        assert isinstance(summary["weekly_series"], str)
        schema = backup.schema()
        assert schema["transaction_table"] == "INOUTCOME"
        assert schema["resolved_columns"]["amount"] == "ZMONEY"
        assert schema["do_type_breakdown"]
    assert parse_mmbak(sample_mmbak)["status"] == "ok"
    assert _month_label(date(2026, 2, 3)) == "2026-02"
    assert _iso_week_start(date(2026, 2, 4)).weekday() == 0


def test_cli_smoke(sample_mmbak: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["summary", str(sample_mmbak)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert main(["schema", str(sample_mmbak)]) == 0
    assert json.loads(capsys.readouterr().out)["transaction_table"] == "INOUTCOME"
    assert main(["query", str(sample_mmbak), "--top", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["top"]
    assert main(["summary", str(sample_mmbak.with_name("missing.mmbak"))]) == 1
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
