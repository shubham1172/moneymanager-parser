from __future__ import annotations

import sqlite3
import zipfile
from datetime import date
from pathlib import Path

import pytest


def _month_delta(day: date, months: int) -> date:
    month = day.month + months
    year = day.year
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return date(year, month, min(day.day, 28))


def _write_zip(db_path: Path, archive_path: Path) -> Path:
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(db_path, "money.sqlite")
    return archive_path


@pytest.fixture()
def sample_mmbak(tmp_path: Path) -> Path:
    db = tmp_path / "sample.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE ZCATEGORY (uid TEXT, C_UID TEXT, ZNAME TEXT);
        CREATE TABLE ASSETS (uid TEXT, ZNAME TEXT, ZMONEY TEXT);
        CREATE TABLE INOUTCOME (
            ZMONEY TEXT, WDATE TEXT, ZDATE TEXT, DO_TYPE TEXT,
            ctgUid TEXT, CATEGORY_NAME TEXT, assetUid TEXT, ZCONTENT TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO ZCATEGORY VALUES (?, ?, ?)",
        [("1", "c1", "Food"), ("2", "c2", "Transport"), ("3", "c3", "Salary")],
    )
    con.executemany(
        "INSERT INTO ASSETS VALUES (?, ?, ?)", [("a1", "Cash", "1000"), ("a2", "Bank", "2500")]
    )
    today = date.today()
    rows = [
        ("120.50", today.isoformat(), "", "1", "1", None, "a1", "test coffee"),
        ("80", today.isoformat(), "", "1", "2", "Transit", "a1", "test train"),
        ("2500", today.isoformat(), "", "0", "3", None, "a2", "test salary"),
        ("999", today.isoformat(), "", "3", "1", None, "a1", "test transfer"),
        ("300", _month_delta(today, -1).isoformat(), "", "1", "1", None, "a1", "test groceries"),
        ("2000", _month_delta(today, -1).isoformat(), "", "0", "3", None, "a2", "test income"),
        ("45", _month_delta(today, -2).isoformat(), "", "7", "2", None, "a1", "test unmapped"),
    ]
    con.executemany("INSERT INTO INOUTCOME VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return _write_zip(db, tmp_path / "sample.mmbak")


@pytest.fixture()
def raw_sqlite(tmp_path: Path, sample_mmbak: Path) -> Path:
    raw = tmp_path / "raw.sqlite"
    with zipfile.ZipFile(sample_mmbak) as archive:
        raw.write_bytes(archive.read("money.sqlite"))
    return raw


@pytest.fixture()
def alternate_mmbak(tmp_path: Path) -> Path:
    db = tmp_path / "alternate.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE category (id TEXT, name TEXT);
        CREATE TABLE assets (id TEXT, name TEXT, balance TEXT);
        CREATE TABLE inoutcome (amount TEXT, date TEXT, type TEXT, category_id TEXT, account_id TEXT, memo TEXT);
        """
    )
    con.execute("INSERT INTO category VALUES ('10', 'Food')")
    con.execute("INSERT INTO assets VALUES ('20', 'Wallet', '42')")
    con.execute(
        "INSERT INTO inoutcome VALUES ('12', '20260103', 'expense', '10', '20', 'test snack')"
    )
    con.commit()
    con.close()
    return _write_zip(db, tmp_path / "alternate.mmbak")
