"""Command line interface for moneymanager-parser."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Any

from .core import MoneyManagerBackup


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mmbak", description="Offline Realbyte .mmbak parser")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("summary", "schema"):
        cmd = sub.add_parser(name)
        cmd.add_argument("path")
    query = sub.add_parser("query")
    query.add_argument("path")
    query.add_argument("--from", dest="date_from")
    query.add_argument("--to", dest="date_to")
    query.add_argument("--month")
    query.add_argument("--category")
    query.add_argument("--search")
    query.add_argument("--kind", choices=["expense", "income", "all"], default="expense")
    query.add_argument("--group-by", choices=["month", "category", "day", "week"])
    query.add_argument("--top", type=int)
    query.add_argument("--list", dest="list_n", type=int)
    query.add_argument("--limit", type=int, default=24)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with MoneyManagerBackup.from_file(args.path) as backup:
            if args.command == "summary":
                _print(backup.summary().as_dict())
            elif args.command == "schema":
                _print(backup.schema())
            else:
                _print(
                    backup.query(
                        date_from=args.date_from,
                        date_to=args.date_to,
                        month=args.month,
                        category=args.category,
                        search=args.search,
                        kind=args.kind,
                        group_by=args.group_by,
                        top=args.top,
                        list_n=args.list_n,
                        limit=args.limit,
                    ).as_dict()
                )
    except Exception as exc:  # noqa: BLE001
        _print({"status": "error", "reason": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
