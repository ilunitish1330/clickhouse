"""Reviewing an upload: its rows as they will be after the pending edits, and the edits.

An upload is live on the dashboards at once, as a Draft (ub_load.py). A reviewer edits
its rows here; every edit waits in ub.raw_pending (the whole new row, latest image per
line) and is written to ub.edit_log cell by cell. Nothing reaches the dashboards until
"Finalize modified data" runs `ub_load.py --apply`, which swaps the edited rows into
ub.raw_rows and rebuilds that batch's aggregates. "Reviewed" then makes the batch Final;
editing a Final batch starts the next Draft revision.

Every value placed into SQL goes through sql_list() (quoted and escaped) or int().
"""
import json
import re

import ax_load
from ub_load import COLS, sql_list

EDITABLE = [c for c in COLS if c not in ("BatchId", "LoadDateTime")]
DEFAULT_VIEW = ["CUSTID", "CONNECTIONID", "INVOICEID", "UTILITYTYPE", "REGION", "TARIFFGROUPCODE",
                "TARIFFGROUPDESC", "SECTORDESCRIPTION", "ADDITIONALITEMSDESCRIPTION", "QUANTITY", "AMOUNT"]
LABELS = {
    "CUSTID": "Customer", "CONNECTIONID": "Connection", "INVOICEID": "Invoice", "UTILITYTYPE": "Utility",
    "REGION": "Island", "TARIFFGROUPCODE": "Tariff code", "TARIFFGROUPDESC": "Tariff group",
    "SECTORDESCRIPTION": "Sector", "SECTORGROUP": "Sector code", "ADDITIONALITEMSDESCRIPTION": "Billing item",
    "ADDITIONALITEMSTYPE": "Item type", "QUANTITY": "Quantity", "AMOUNT": "Amount", "INVOICEDATE": "Invoice date",
    "CURDATE": "Statistics date", "CURDATETICKS": "Date ticks", "MCSEXTERNALASSETID": "Meter",
    "STATCATEGORIES": "Category id", "CATEGORYDESCRIPTION": "Category", "INVOICEORIGIN": "Origin code",
    "INVOICEORIGINDESCRIPTION": "Invoice origin", "INVOICEVALUETYPE": "Value type code",
    "INVOICEVALUEDESCRIPTION": "Value type", "UTILITYTYPEDESCRIPTION": "Utility name", "REGIONNAME": "Island name",
    "CONNECTIONMEMBERTYPE": "Member type", "FREETEXTINVOICE": "Free text", "PVINDICATION": "PV",
    "METERWATERNODE": "Water node", "METERWATERSOURCE": "Water source", "BatchId": "Batch", "LoadDateTime": "Loaded",
}
NUMBER = {"AMOUNT", "QUANTITY", "CURDATETICKS"}
WHOLE = {"ADDITIONALITEMSTYPE", "INVOICEORIGIN", "INVOICEVALUETYPE", "FREETEXTINVOICE", "CONNECTIONMEMBERTYPE",
         "PVINDICATION"}
CODES = {"UTILITYTYPE": (1, 2, 3), "REGION": (1, 2, 3)}
DATES = {"INVOICEDATE", "CURDATE"}
# a changed code brings its labels along, from the latest row that has that code
COMPANIONS = {"UTILITYTYPE": ["UTILITYTYPEDESCRIPTION"], "REGION": ["REGIONNAME"],
              "TARIFFGROUPCODE": ["TARIFFGROUPDESC", "STATCATEGORIES", "CATEGORYDESCRIPTION", "SECTORGROUP",
                                  "SECTORDESCRIPTION"]}
SEARCHED = ["CUSTID", "CONNECTIONID", "INVOICEID", "MCSEXTERNALASSETID", "TARIFFGROUPCODE", "TARIFFGROUPDESC"]


class Invalid(ValueError):
    pass


def q(sql: str) -> list[dict]:
    return json.loads(ax_load.ch(sql + " FORMAT JSON"))["data"]


def columns() -> dict:
    return {"all": COLS, "editable": EDITABLE, "default": DEFAULT_VIEW, "labels": LABELS,
            "number": sorted(NUMBER), "whole": sorted(WHOLE), "codes": {k: list(v) for k, v in CODES.items()},
            "dates": sorted(DATES)}


def validate(col: str, value) -> str:
    if col not in EDITABLE:
        raise Invalid(f"{LABELS.get(col, col)} cannot be edited")
    v = str(value if value is not None else "").replace("\t", " ").replace("\n", " ").strip()
    if len(v) > 500:
        raise Invalid(f"{LABELS.get(col, col)}: at most 500 characters")
    if col in NUMBER and v and not re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", v):
        raise Invalid(f"{LABELS[col]} must be a number, like 1250.75")
    if col in WHOLE and v and not re.fullmatch(r"\d{1,3}", v):
        raise Invalid(f"{LABELS[col]} must be a whole number")
    if col in CODES and v not in {str(c) for c in CODES[col]}:
        raise Invalid(f"{LABELS[col]} must be one of {', '.join(str(c) for c in CODES[col])}")
    if col in DATES and v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?", v):
        raise Invalid(f"{LABELS[col]} must be a date as yyyy-mm-dd")
    if col in ("AMOUNT", "QUANTITY") and not v:
        raise Invalid(f"{LABELS[col]} cannot be empty")
    return v


# ---------------------------------------------------------------------------- reads

def batches() -> list[dict]:
    return q("""
        SELECT r.batch_id AS batch_id, r.file_name AS file_name, toString(r.batch_month) AS batch_month,
               r.rows AS raw_rows, toString(r.loaded_at) AS loaded_at,
               ifNull(s.status, 'draft') AS status, ifNull(s.revision, 1) AS revision,
               ifNull(s.changed_by, '') AS changed_by, ifNull(toString(s.last_change), '') AS last_change,
               ifNull(f.period, '') AS period, ifNull(f.lines, 0) AS lines, ifNull(f.amount, 0) AS amount,
               ifNull(p.pending, 0) AS pending
        FROM (SELECT batch_id, any(file_name) AS file_name, any(batch_month) AS batch_month, sum(rows) AS rows,
                     max(loaded_at) AS loaded_at FROM ub.raw_batches GROUP BY batch_id) AS r
        LEFT JOIN ub.v_batch_status AS s ON s.batch_id = r.batch_id
        LEFT JOIN (SELECT batch_id, toString(min(period)) AS period, count() AS lines, toFloat64(sum(amount)) AS amount
                   FROM ub.fact_lines GROUP BY batch_id) AS f ON f.batch_id = r.batch_id
        LEFT JOIN (SELECT batch_id, count() AS pending FROM ub.raw_pending FINAL GROUP BY batch_id) AS p
               ON p.batch_id = r.batch_id
        ORDER BY r.batch_month DESC, r.batch_id
        SETTINGS join_use_nulls = 1""")


def batch(batch_id: str) -> dict:
    b = next((x for x in batches() if x["batch_id"] == batch_id), None)
    if not b:
        raise Invalid("No such batch")
    return b


def _merged(batch_id: str) -> str:
    """The batch's rows as they will be once the pending edits are finalized, plus the pending state."""
    b = sql_list([batch_id])
    cols = ", ".join(f"if(p.edited_by != '', p.{c}, r.{c}) AS {c}" for c in COLS)
    raw = ", ".join(COLS)
    return f"""(
        SELECT r.line_no AS line_no, if(p.edited_by != '', p.action, '') AS pending, {cols}
        FROM (SELECT line_no, {raw} FROM ub.raw_rows WHERE batch_id = {b}) AS r
        LEFT JOIN (SELECT * FROM ub.raw_pending FINAL WHERE batch_id = {b}) AS p ON p.line_no = r.line_no
        UNION ALL
        SELECT line_no, 'insert' AS pending, {raw} FROM ub.raw_pending FINAL WHERE batch_id = {b} AND action = 'insert')"""


def rows(batch_id: str, search: str = "", changed: bool = False, page: int = 1, size: int = 50,
         utility: int = 0, region: int = 0, rule: dict | None = None) -> dict:
    conds = ["1"]
    if rule:  # the formula builder's "Which rows", to see exactly what it matches
        import formula
        conds.append(f"({formula.compile_rule(rule)}) AND pending != 'delete'")
    if search.strip():
        s = sql_list([search.strip()])
        conds.append("(" + " OR ".join(f"positionCaseInsensitive({c}, {s}) > 0" for c in SEARCHED) + ")")
    if changed:
        conds.append("pending != ''")
    if utility in (1, 2, 3):
        conds.append(f"UTILITYTYPE = '{int(utility)}'")
    if region in (1, 2, 3):
        conds.append(f"REGION = '{int(region)}'")
    where = " AND ".join(conds)
    size = max(10, min(int(size), 200))
    page = max(1, int(page))
    src = _merged(batch_id)
    total = int(q(f"SELECT count() AS n FROM {src} WHERE {where} SETTINGS prefer_column_name_to_alias = 1")[0]["n"])
    got = q(f"SELECT * FROM {src} WHERE {where} ORDER BY pending = '' , line_no "
            f"LIMIT {size} OFFSET {(page - 1) * size} SETTINGS prefer_column_name_to_alias = 1")
    # for edited rows, what each changed cell was before
    edited = [int(r["line_no"]) for r in got if r["pending"] in ("update", "delete")]
    before = {}
    if edited:
        for r in q(f"SELECT line_no, {', '.join(COLS)} FROM ub.raw_rows WHERE batch_id = {sql_list([batch_id])} "
                   f"AND line_no IN ({', '.join(str(n) for n in edited)})"):
            before[int(r["line_no"])] = r
    out = []
    for r in got:
        n = int(r["line_no"])
        old = before.get(n, {})
        out.append({"line_no": n, "pending": r["pending"], "values": {c: r[c] for c in COLS},
                    "changed": {c: old[c] for c in COLS if old and r["pending"] == "update" and old[c] != r[c]}})
    return {"total": total, "page": page, "size": size, "rows": out}


def history(batch_id: str, limit: int = 300) -> list[dict]:
    # a formula's cell-by-cell entries (bulk_*) stay in the log; its one 'formula' entry stands for them here
    return q(f"SELECT toString(ts) AS ts, line_no, action, column, old_value, new_value, user, revision "
             f"FROM ub.edit_log WHERE batch_id = {sql_list([batch_id])} AND NOT startsWith(action, 'bulk_') "
             f"ORDER BY ts DESC LIMIT {int(limit)}")


# ---------------------------------------------------------------------------- edits

def _current(batch_id: str, line_no: int) -> tuple[dict | None, str]:
    """The row as the reviewer sees it now, and its pending action ('' = untouched)."""
    b = sql_list([batch_id])
    p = q(f"SELECT action, {', '.join(COLS)} FROM ub.raw_pending FINAL WHERE batch_id = {b} AND line_no = {int(line_no)}")
    if p:
        return {c: p[0][c] for c in COLS}, p[0]["action"]
    r = q(f"SELECT {', '.join(COLS)} FROM ub.raw_rows WHERE batch_id = {b} AND line_no = {int(line_no)}")
    return ({c: r[0][c] for c in COLS}, "") if r else (None, "")


def _original(batch_id: str, line_no: int) -> dict | None:
    r = q(f"SELECT {', '.join(COLS)} FROM ub.raw_rows WHERE batch_id = {sql_list([batch_id])} AND line_no = {int(line_no)}")
    return {c: r[0][c] for c in COLS} if r else None


def _write(batch_id: str, line_no: int, action: str, values: dict, user: str) -> None:
    cols = ["batch_id", "line_no", "action"] + COLS + ["edited_by"]
    vals = [batch_id, str(line_no), action] + [values.get(c, "") for c in COLS] + [user]
    ax_load.ch(f"INSERT INTO ub.raw_pending ({', '.join(cols)}) VALUES "
               f"({sql_list(vals[:1])}, {int(line_no)}, {sql_list(vals[2:])})")


def _log(batch_id: str, user: str, action: str, line_no: int = 0, column: str = "", old: str = "", new: str = "") -> None:
    rev = q(f"SELECT revision FROM ub.v_batch_status WHERE batch_id = {sql_list([batch_id])}")
    ax_load.ch("INSERT INTO ub.edit_log (batch_id, line_no, action, column, old_value, new_value, user, revision) VALUES "
               f"({sql_list([batch_id])}, {int(line_no)}, {sql_list([action, column, old, new, user])}, "
               f"{int(rev[0]['revision']) if rev else 1})")


def _drop_pending(batch_id: str, line_no: int) -> None:
    ax_load.ch(f"DELETE FROM ub.raw_pending WHERE batch_id = {sql_list([batch_id])} AND line_no = {int(line_no)}")


def _fill_companions(changes: dict) -> dict:
    """A changed code brings the labels that go with it (unless they were changed too)."""
    out = dict(changes)
    for code, labels in COMPANIONS.items():
        if code in changes and not all(lbl in changes for lbl in labels):
            ref = q(f"SELECT {', '.join(labels)} FROM ub.raw_rows WHERE {code} = {sql_list([changes[code]])} "
                    f"ORDER BY batch_id DESC, line_no DESC LIMIT 1")
            if ref:
                for lbl in labels:
                    out.setdefault(lbl, ref[0][lbl])
    return out


def edit(batch_id: str, line_no: int, changes: dict, user: str) -> dict:
    cur, action = _current(batch_id, line_no)
    if cur is None:
        raise Invalid("No such row")
    if action == "delete":
        raise Invalid("This row is marked for deletion: undo that first")
    clean = _fill_companions({c: validate(c, v) for c, v in changes.items()})
    new = {**cur, **clean}
    if new == cur:
        return {"ok": True, "unchanged": True}
    for c in clean:
        if cur[c] != new[c]:
            _log(batch_id, user, "update" if action != "insert" else "insert", line_no, c, cur[c], new[c])
    if action != "insert" and new == _original(batch_id, line_no):
        _drop_pending(batch_id, line_no)  # edited back to what it was: nothing pending
    else:
        _write(batch_id, line_no, action or "update", new, user)
    return {"ok": True}


def add(batch_id: str, values: dict, user: str) -> dict:
    batch(batch_id)
    clean = _fill_companions({c: validate(c, v) for c, v in values.items() if c in EDITABLE})
    for need in ("UTILITYTYPE", "REGION", "AMOUNT", "QUANTITY"):
        if not clean.get(need):
            raise Invalid(f"{LABELS[need]} is required for a new row")
    b = sql_list([batch_id])
    n = int(q(f"SELECT greatest((SELECT max(line_no) FROM ub.raw_rows WHERE batch_id = {b}), "
              f"(SELECT max(line_no) FROM ub.raw_pending WHERE batch_id = {b})) + 1 AS n")[0]["n"])
    row = {c: "" for c in COLS} | clean | {"BatchId": batch_id}
    _write(batch_id, n, "insert", row, user)
    _log(batch_id, user, "insert", n, "", "", f"{row['AMOUNT']} ({row['CUSTID'] or 'no customer'})")
    return {"ok": True, "line_no": n}


def delete(batch_id: str, line_no: int, user: str) -> dict:
    cur, action = _current(batch_id, line_no)
    if cur is None:
        raise Invalid("No such row")
    if action == "insert":  # a row added in this draft: just drop it
        _drop_pending(batch_id, line_no)
    else:
        _write(batch_id, line_no, "delete", _original(batch_id, line_no) or cur, user)
    _log(batch_id, user, "delete", line_no, "", f"{cur['AMOUNT']} ({cur['CUSTID']})", "")
    return {"ok": True}


def undo(batch_id: str, line_no: int, user: str) -> dict:
    _cur, action = _current(batch_id, line_no)
    if not action:
        raise Invalid("This row has no pending change")
    _drop_pending(batch_id, line_no)
    _log(batch_id, user, "undo", line_no, "", action, "")
    return {"ok": True}


def discard(batch_id: str, user: str) -> dict:
    n = int(q(f"SELECT count() AS n FROM ub.raw_pending FINAL WHERE batch_id = {sql_list([batch_id])}")[0]["n"])
    ax_load.ch(f"DELETE FROM ub.raw_pending WHERE batch_id = {sql_list([batch_id])}")
    _log(batch_id, user, "discard", 0, "", f"{n} row(s)", "")
    return {"ok": True, "discarded": n}


def reviewed(batch_id: str, user: str) -> dict:
    b = batch(batch_id)
    if int(b["pending"]):
        raise Invalid("Finalize or discard the pending edits before marking the data reviewed")
    if b["status"] == "final":
        raise Invalid("This batch is already reviewed")
    ax_load.ch("INSERT INTO ub.batch_events (batch_id, status, revision, changed_by, note) VALUES "
               f"({sql_list([batch_id])}, 'final', {int(b['revision'])}, {sql_list([user])}, 'reviewed')")
    _log(batch_id, user, "reviewed")
    return {"ok": True}


def status_by_period() -> dict:
    """period -> [{batch_id, status, revision}] for the dashboards' status line."""
    out = {}
    for r in q("SELECT toString(f.period) AS period, f.batch_id AS batch_id, ifNull(s.status, 'draft') AS status, "
               "ifNull(s.revision, 1) AS revision, ifNull(toString(s.last_change), '') AS last_change, "
               "ifNull(s.changed_by, '') AS changed_by "
               "FROM (SELECT DISTINCT period, batch_id FROM ub.fact_lines) AS f "
               "LEFT JOIN ub.v_batch_status AS s ON s.batch_id = f.batch_id SETTINGS join_use_nulls = 1"):
        out.setdefault(r["period"], []).append(r)
    return out
