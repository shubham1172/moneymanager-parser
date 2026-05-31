# moneymanager-parser

[![CI](https://github.com/shubham1172/moneymanager-parser/actions/workflows/ci.yml/badge.svg)](https://github.com/shubham1172/moneymanager-parser/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/moneymanager-parser)](https://pypi.org/project/moneymanager-parser/)
[![Python versions](https://img.shields.io/pypi/pyversions/moneymanager-parser)](https://pypi.org/project/moneymanager-parser/)

Typed Python SDK and offline CLI for Realbyte Money Manager `.mmbak` exports.

A `.mmbak` export is a ZIP-wrapped SQLite database. This package reads the export locally,
resolves common schema aliases across app versions, and exposes transactions, summaries, flexible
queries, schema inspection, categories, accounts, and currencies. Core parsing uses only the Python
standard library. Income is exposed by the API, but expense-oriented analysis should treat it as
informational because Money Manager exports can vary by installation.

The backup's currencies are read from the `CURRENCY` table when present. `currency()` returns the main
currency (`ISO`, `symbol`, `name`); amounts are stored in the account/transaction currency and are not
converted.

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
    print(backup.query(month="2026-01", group_by="category").as_dict())
```

Custom transaction type maps are supported:

```python
MoneyManagerBackup.from_file("backup.mmbak", type_map={0: "income", 1: "expense", 7: "transfer"})
```

## CLI

```bash
mmbak query backup.mmbak --month 2026-01 --group-by category --top 10
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
