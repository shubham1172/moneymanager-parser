"""Schema aliases used by Realbyte Money Manager releases."""

TXN_TABLE_CANDIDATES = ("INOUTCOME", "ZINOUTCOME", "inoutcome")
CATEGORY_TABLE_CANDIDATES = ("ZCATEGORY", "CATEGORY", "category")
ASSET_TABLE_CANDIDATES = ("ASSETS", "ZASSETS", "ASSET", "assets")
CURRENCY_TABLE_CANDIDATES = ("CURRENCY", "ZCURRENCY", "currency")

AMOUNT_COLS = ("ZMONEY", "AMOUNT", "amount", "money", "ZAMOUNT")
DATE_COLS = ("WDATE", "wdate", "ZDATE", "DATE", "date", "ZTIME")
TYPE_COLS = ("DO_TYPE", "type", "TYPE", "ZTYPE", "inoutType")
CATFK_COLS = ("ctgUid", "CTGUID", "ZCATEGORY", "category_id", "categoryUid", "ctg_uid")
CATNAME_COLS = ("CATEGORY_NAME", "category_name", "ZCTGNAME", "ctgName")
ASSETFK_COLS = ("assetUid", "ASSETUID", "ASSETS", "account_id", "asset_uid")
MEMO_COLS = ("ZCONTENT", "MEMO", "memo", "content", "ZCOMMENT", "comment")

NAME_COLS = ("ZNAME", "NAME", "name", "title", "ZTITLE")
UID_COLS = ("uid", "UID", "C_UID", "_id", "id", "ID", "ZUID")
BALANCE_COLS = ("ZMONEY", "AMOUNT", "amount", "balance", "ZAMOUNT")

CURRENCY_ISO_COLS = ("ISO", "iso", "CODE", "code", "ZISO")
CURRENCY_SYMBOL_COLS = ("SYMBOL", "symbol", "ZSYMBOL")
CURRENCY_MAIN_COLS = ("IS_MAIN_CURRENCY", "is_main_currency", "IS_MAIN", "ZISMAIN")

DEFAULT_TYPE_MAP = {0: "income", 1: "expense", 2: "transfer", 3: "transfer", 4: "transfer"}
STRING_TYPE_MAP = {
    "income": "income",
    "in": "income",
    "0": "income",
    "expense": "expense",
    "out": "expense",
    "1": "expense",
    "transfer": "transfer",
    "trans": "transfer",
    "2": "transfer",
    "3": "transfer",
}
