# moneymanager-parser

[![CI](https://github.com/shubham1172/moneymanager-parser/actions/workflows/ci.yml/badge.svg)](https://github.com/shubham1172/moneymanager-parser/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/moneymanager-parser)](https://pypi.org/project/moneymanager-parser/)
[![Python versions](https://img.shields.io/pypi/pyversions/moneymanager-parser)](https://pypi.org/project/moneymanager-parser/)

Typed Python SDK and offline CLI for Realbyte Money Manager `.mmbak` exports.

A `.mmbak` export is a ZIP-wrapped SQLite database. This package reads the export locally,
resolves common schema aliases across app versions, and exposes transactions, flexible
queries, schema inspection, categories, accounts, and currencies. Core parsing uses only the Python
standard library. Income is exposed by the API, but expense-oriented analysis should treat it as
informational because Money Manager exports can vary by installation.

The backup's currencies are read from the `CURRENCY` table when present. `currency()` returns the main
currency (`ISO`, `symbol`, `name`); amounts are stored in the account/transaction currency and are not
converted.

Soft-deleted rows (`IS_DEL` / `C_IS_DEL`) are excluded from all results. Account names resolve from
the asset table (including the `NIC_NAME` nickname used by recent exports). When the asset table has an
explicit balance column it is used as-is; otherwise `accounts()` reports a best-effort balance derived
from transactions (income adds, expense subtracts, transfers move the amount to the destination account
via `toAssetUid`). Derived balances are raw numeric sums with no currency conversion, so totals mixing
currencies are not meaningful.

### Safety

All connections are opened read-only (`PRAGMA query_only=ON`) and never contact external services.
Archive members are size-checked before extraction to guard against decompression bombs, and all SQL
identifiers read from the backup are quoted defensively.

## Install

```bash
pip install moneymanager-parser
```

## Quickstart

```python
from moneymanager_parser import MoneyManagerBackup

with MoneyManagerBackup.from_file("backup.mmbak") as backup:
    main = backup.currency()
    if main:
        print(main.iso, main.symbol)
    for txn in backup.transactions():
        print(txn.date, txn.kind, txn.category, txn.amount)
    # Filter or group by account for account-specific totals.
    print(backup.query(month="2026-01", account="Credit Card", group_by="account").as_dict())
    print(backup.query(month="2026-01", top=10).as_dict())  # rows include account/to_account
```

Custom transaction type maps are supported:

```python
MoneyManagerBackup.from_file("backup.mmbak", type_map={0: "income", 1: "expense", 7: "transfer"})
```

## CLI

```bash
mmbak query backup.mmbak --month 2026-01 --account "Credit Card" --group-by account --top 10
mmbak schema backup.mmbak
mmbak currency backup.mmbak
```

All commands print JSON and never contact external services.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff check .
ruff format --check .
mypy src
pytest --cov
```

## Contributing

Issues and pull requests are welcome. Please include tests for schema variants and avoid committing
personal exports; test backups should be generated synthetically.

## License

MIT © 2026 Shubham Sharma
