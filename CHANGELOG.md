# Changelog

All notable changes to this project are documented here.

## [Unreleased]

## [0.4.0] - 2026-07-06

### Added
- `MoneyManagerBackup.query(..., account=...)` filters rows by case-insensitive
  exact account name and records the account in result filters.
- `group_by="account"` returns totals per source account, with missing accounts
  grouped as `Unspecified`.

## [0.3.0] - 2026-07-06

### Changed
- Query `top` and `transactions` rows now include `account` and `to_account`
  for each serialized transaction.

## [0.2.0] - 2026-06-01

### Added
- `Transaction.to_account` exposing the transfer destination account (`toAssetUid`).
- Account name resolution for the `NIC_NAME` nickname used by recent exports.
- Best-effort derived account balances when the asset table has no balance column.

### Changed
- Soft-deleted rows (`IS_DEL` / `C_IS_DEL`) are now excluded from transactions, accounts,
  categories, and currencies.
- `transactions()`, `accounts()`, and `currencies()` results are memoized per backup.

### Security
- Connections are opened read-only via `PRAGMA query_only=ON`.
- Archive members are size-checked before extraction to guard against decompression bombs.
- SQL identifiers read from the backup are quoted defensively.

## [0.1.0] - 2026-06-01

### Added
- Initial typed SDK for reading Realbyte Money Manager `.mmbak` exports.
- Query, schema, category, account, and currency APIs.
- Offline JSON CLI exposed as `mmbak`.
