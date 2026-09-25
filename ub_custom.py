"""Columns a reviewer adds to the billing data (Data Review -> Formula builder -> Add a column).

Each row keeps the values of added columns in `extra`, a Map(String, String) on ub.raw_rows,
under a stable key (c1, c2, ...). The key never changes and is never reused, so renaming or
removing a column cannot mix up values. ub.custom_columns holds the definitions; the view
ub.v_custom shows the finalized billing lines with every active added column as a real,
typed, named column -- what to query in ClickHouse or import into Power BI.

An upload whose file has a column with the same name as an added column fills it.
"""
import json
import re

import ax_load

KINDS = {"number": "Number", "text": "Text", "date": "Date (yyyy-mm-dd)"}
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9 _\-()/%.]{0,39}")


def q(sql: str) -> list[dict]:
    return json.loads(ax_load.ch(sql + " FORMAT JSON"))["data"]


def text(v) -> str:
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def columns(include_removed: bool = False) -> list[dict]:
    """[{key, name, kind, created_by, changed_at, deleted}], in the order they were added."""
    try:
        rows = q("SELECT key, name, kind, created_by, toString(changed_at) AS changed_at, deleted "
                 "FROM ub.custom_columns FINAL ORDER BY toUInt32OrZero(substring(key, 2))")
    except Exception:
        return []  # before the first schema run
    return [r for r in rows if include_removed or not int(r["deleted"])]


def add(name: str, kind: str, user: str, reserved: set) -> dict:
    """A new column. reserved: names already taken by the export's own columns (lower case)."""
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not NAME_RE.fullmatch(name):
        raise ValueError("A column name starts with a letter and has up to 40 letters, digits, spaces or - _ ( ) / % .")
    if kind not in KINDS:
        raise ValueError("Pick a type: number, text or date")
    taken = reserved | {c["name"].lower() for c in columns()}
    if name.lower() in taken:
        raise ValueError(f"There is already a column called {name}")
    used = [int(c["key"][1:]) for c in columns(include_removed=True) if c["key"][1:].isdigit()]
    key = f"c{max(used, default=0) + 1}"
    ax_load.ch("INSERT INTO ub.custom_columns (key, name, kind, created_by) VALUES "
               f"({text(key)}, {text(name)}, {text(kind)}, {text(user)})")
    refresh_view()
    return {"key": key, "name": name, "kind": kind}


def remove(key: str, user: str) -> None:
    col = next((c for c in columns() if c["key"] == key), None)
    if not col:
        raise ValueError("No such column")
    ax_load.ch("INSERT INTO ub.custom_columns (key, name, kind, created_by, deleted) VALUES "
               f"({text(key)}, {text(col['name'])}, {text(col['kind'])}, {text(user)}, 1)")
    refresh_view()


def value_sql(key: str, kind: str, source: str = "extra") -> str:
    v = f"{source}[{text(key)}]"
    if kind == "number":
        return f"toFloat64OrNull({v})"
    if kind == "date":
        return f"toDateOrNull({v})"
    return v


def refresh_view() -> None:
    """ub.v_custom: the finalized billing lines with each active added column as a real column."""
    cols = "".join(f",\n    {value_sql(c['key'], c['kind'])} AS `{c['name']}`" for c in columns())
    ax_load.ch(f"""CREATE OR REPLACE VIEW ub.v_custom AS
SELECT batch_id, line_no, period, utility_code, utility_name, region_code, region_name, customer_id,
       connection_id, invoice_id, tariff_code, tariff_desc, sector_type, amount, quantity{cols}
FROM ub.fact_lines""")
