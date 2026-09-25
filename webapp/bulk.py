"""The formula builder's bulk edits: a rule picks rows of one batch, an action changes them.

    spec = {"rule":   {"combine": "and", "rules": [{"field": "REGION", "op": "eq", "value": "2"}, ...]},
            "action": "update" | "copy" | "delete" | "add_column",
            "sets":   [{"field": "AMOUNT", "formula": "[Amount] * 1.05"}, ...]}

update      sets the fields on every matching row
copy        adds a copy of every matching row, with the fields set
delete      deletes every matching row
add_column  adds a column ({"column": {"name", "kind"}, "formula"}): the formula fills the
            matching rows, every other row starts blank. Added columns are "x:<key>" fields
            (ub_custom.py) and can be set, and used in rules and formulas, like any other.

Rows are matched as the reviewer sees them (pending edits included). Nothing reaches the
dashboards here: the result is written to ub.raw_pending like any edit and waits for "Finalize
modified data". The change log gets one 'formula' entry (the rule and the formulas) and one
bulk_* entry per changed cell. preview() shows what apply() would do.
"""
import json

import ax_load
import formula as F
import ub_custom
from review import CODES, COLS, EDITABLE, LABELS, Invalid, _merged, q
from ub_load import sql_list

ACTIONS = {"update": "Change values", "copy": "Copy as new rows", "delete": "Delete rows", "add_column": "Add a column"}
MAX_SETS = 20
MAX_COPY = 100_000
COMPANIONS = {"UTILITYTYPE": ["UTILITYTYPEDESCRIPTION"], "REGION": ["REGIONNAME"],
              "TARIFFGROUPCODE": ["TARIFFGROUPDESC", "STATCATEGORIES", "CATEGORYDESCRIPTION", "SECTORGROUP",
                                  "SECTORDESCRIPTION"]}
NEW = "__new__"  # the key of a column being added, while it is previewed
RESERVED = {c.lower() for c in COLS} | {v.lower() for v in LABELS.values()}


def alias(col: str) -> str:
    """A column's name inside the plan's SQL: AMOUNT, or x_c1 for an added column."""
    return col.replace(":", "_")


def check(spec: dict) -> dict:
    """A clean spec, or Invalid / FormulaError saying what is wrong.

    add_column: {"action": "add_column", "column": {"name", "kind"}, "formula": optional, "rule"}
    -- the rule picks the rows the formula fills; every other row starts blank."""
    if not isinstance(spec, dict):
        raise Invalid("Malformed request")
    action = spec.get("action")
    if action not in ACTIONS:
        raise Invalid("Pick what to do: change values, copy as new rows, delete rows, or add a column")
    column = None
    if action == "add_column":
        col = spec.get("column") or {}
        name = " ".join(str(col.get("name", "")).split())
        kind = col.get("kind")
        if not ub_custom.NAME_RE.fullmatch(name):
            raise Invalid("A column name starts with a letter and has up to 40 letters, digits, spaces or - _ ( ) / % .")
        if kind not in ub_custom.KINDS:
            raise Invalid("Pick the new column's type: number, text or date")
        if name.lower() in RESERVED | {c["name"].lower() for c in ub_custom.columns()}:
            raise Invalid(f"There is already a column called {name}")
        column = {"name": name, "kind": kind}
        F.load_custom({"key": NEW, "name": name, "kind": kind})
        src = str(spec.get("formula") or "").strip()
        spec = {**spec, "sets": [{"field": f"x:{NEW}", "formula": src}] if src else []}
    else:
        F.load_custom()
    sets = []
    if action != "delete":
        seen = set()
        for s in spec.get("sets") or []:
            col = F.field(str(s.get("field", "")))
            if col not in EDITABLE and not F.is_custom(col):
                raise Invalid(f"{F.label(col)} cannot be changed")
            if col in seen:
                raise Invalid(f"{F.label(col)} is set twice")
            seen.add(col)
            src = str(s.get("formula", "")).strip()
            try:
                F.value_sql(col, src)
            except F.FormulaError as e:
                e.args = (f"{F.label(col)}: {e}",)
                e.field = s.get("field") if action != "add_column" else "new"
                raise
            sets.append({"field": col, "formula": src})
        if action == "update" and not sets:
            raise Invalid("Add at least one field to change")
        if len(sets) > MAX_SETS:
            raise Invalid(f"At most {MAX_SETS} fields at a time")
    rule = spec.get("rule") or {"combine": "and", "rules": []}
    F.compile_rule(rule)
    out = {"rule": rule, "action": action, "sets": sets}
    if column:
        out["column"] = column
    return out


def _plan(batch_id: str, spec: dict) -> str:
    """One row per matching row: its line, pending state, o_<col> (now), f_<col> (after), o_extra /
    f_extra (added columns), changed, bad_<i>. Added columns being set also get o_x_<key> / f_x_<key>."""
    cond = F.compile_rule(spec["rule"])
    targets = {s["field"]: s["formula"] for s in spec["sets"]}
    inner_cols = ["m.line_no AS line_no", "m.pending AS pending", "m.extra AS o_extra"] + [f"m.{c} AS o_{c}" for c in COLS]
    inner_cols += [f"{F.value_sql(c, f)} AS n_{alias(c)}" for c, f in targets.items()]
    inner_cols += [f"({F.invalid_sql(c, f)}) AS bad_{i}" for i, (c, f) in enumerate(targets.items())]
    inner = (f"SELECT {', '.join(inner_cols)} FROM {_merged(batch_id)} AS m "
             f"WHERE m.pending != 'delete' AND ({cond})")
    final = {c: f"n_{c}" if c in targets else f"o_{c}" for c in COLS}
    for code, labels in COMPANIONS.items():  # a changed code brings its labels, unless they are set too
        if code in targets:
            for lbl in labels:
                if lbl not in targets:
                    lookup = (f"(SELECT (groupArray(k), groupArray(v)) FROM (SELECT {code} AS k, "
                              f"argMax({lbl}, (batch_id, line_no)) AS v FROM ub.raw_rows GROUP BY k))")
                    final[lbl] = (f"if(n_{code} != o_{code}, transform(n_{code}, tupleElement({lookup}, 1), "
                                  f"tupleElement({lookup}, 2), o_{lbl}), o_{lbl})")
    added = [c for c in targets if F.is_custom(c)]
    if added:  # new values into the map; a blank value leaves no key behind
        pairs = ", ".join(f"{F.sql_text(F.CUSTOM[c]['key'])}, n_{alias(c)}" for c in added)
        f_extra = f"mapFilter((k, v) -> v != '', mapUpdate(o_extra, map({pairs})))"
    else:
        f_extra = "o_extra"
    diffs = [f"({final[c]} != o_{c})" for c in COLS if final[c] != f"o_{c}"] + (["(f_extra != o_extra)"] if added else [])
    changed = " OR ".join(diffs) or "0"
    outer = ", ".join(f"{final[c]} AS f_{c}" for c in COLS)
    per = "".join(f", o_extra[{F.sql_text(F.CUSTOM[c]['key'])}] AS o_{alias(c)}, "
                  f"f_extra[{F.sql_text(F.CUSTOM[c]['key'])}] AS f_{alias(c)}" for c in added)
    bad = "".join(f", bad_{i}" for i in range(len(targets)))
    return (f"SELECT line_no, pending, o_extra, {', '.join(f'o_{c}' for c in COLS)}, {outer}, f_extra{per}, "
            f"({changed}) AS changed{bad} FROM (SELECT *, {f_extra} AS f_extra FROM ({inner}))")


def preview(batch_id: str, spec: dict, sample: int = 20) -> dict:
    spec = check(spec)
    plan = _plan(batch_id, spec)
    targets = [s["field"] for s in spec["sets"]]
    bads = ", ".join(f"countIf(bad_{i}) AS bad_{i}" for i in range(len(targets)))
    fin = lambda c: f"sumIf(toFloat64OrZero({c}), isFinite(toFloat64OrZero({c})))"  # 1/0 must not break the sum
    st = q(f"SELECT count() AS matched, countIf(changed) AS changed, "
           f"{fin('o_AMOUNT')} AS amount_before, {fin('f_AMOUNT')} AS amount_after, "
           f"{fin('o_QUANTITY')} AS quantity_before, {fin('f_QUANTITY')} AS quantity_after"
           f"{', ' + bads if bads else ''} FROM ({plan})")[0]
    total = q(f"SELECT count() AS rows, sum(toFloat64OrZero(AMOUNT)) AS amount FROM {_merged(batch_id)} AS m "
              f"WHERE m.pending != 'delete'")[0]
    matched, before, after = int(st["matched"]), float(st["amount_before"] or 0), float(st["amount_after"] or 0)
    act = spec["action"]
    affected = int(st["changed"]) if act in ("update", "add_column") else matched
    new_total = {"update": float(total["amount"]) - before + after, "delete": float(total["amount"]) - before,
                 "copy": float(total["amount"]) + after, "add_column": float(total["amount"])}[act]
    new_rows = {"update": int(total["rows"]), "delete": int(total["rows"]) - matched,
                "copy": int(total["rows"]) + matched, "add_column": int(total["rows"])}[act]
    invalid = [{"field": c, "label": F.label(c), "rows": int(st[f"bad_{i}"])}
               for i, c in enumerate(targets) if int(st[f"bad_{i}"])]
    shown = list(dict.fromkeys(["CUSTID", "TARIFFGROUPDESC", "REGION"] + targets +
                               [lbl for c in targets for lbl in COMPANIONS.get(c, []) if lbl not in targets][:2] +
                               ["QUANTITY", "AMOUNT"]))
    where = "changed" if act in ("update", "add_column") else "1"
    got = q(f"SELECT line_no, pending, {', '.join(f'o_{alias(c)}, f_{alias(c)}' for c in shown)} FROM ({plan}) "
            f"WHERE {where} ORDER BY line_no LIMIT {int(sample)}")
    rows = [{"line_no": int(r["line_no"]), "pending": r["pending"],
             "before": {c: r[f"o_{alias(c)}"] for c in shown}, "after": {c: r[f"f_{alias(c)}"] for c in shown}} for r in got]
    return {"matched": matched, "affected": affected, "action": act, "rule_text": F.describe_rule(spec["rule"]),
            "amount_before": before, "amount_after": after,
            "quantity_before": float(st["quantity_before"] or 0), "quantity_after": float(st["quantity_after"] or 0),
            "batch_rows": int(total["rows"]), "batch_amount": float(total["amount"]),
            "batch_rows_after": new_rows, "batch_amount_after": new_total,
            "invalid": invalid, "columns": shown, "targets": targets, "sample": rows,
            "labels": {c: F.label(c) for c in shown}, "column": spec.get("column"),
            "too_many": act == "copy" and matched > MAX_COPY,
            "empties_month": act == "delete" and matched >= int(total["rows"])}


def _revision(batch_id: str) -> int:
    r = q(f"SELECT revision FROM ub.v_batch_status WHERE batch_id = {sql_list([batch_id])}")
    return int(r[0]["revision"]) if r else 1


def apply(batch_id: str, spec: dict, user: str) -> dict:
    p = preview(batch_id, spec, sample=0)
    spec = check(spec)
    if p["invalid"]:
        raise Invalid("Some rows would get values their field cannot hold: "
                      + "; ".join(f"{x['label']} on {x['rows']:,} row(s)" for x in p["invalid"]))
    if p["too_many"]:
        raise Invalid(f"At most {MAX_COPY:,} rows can be copied at once")
    if p["empties_month"]:
        raise Invalid("This would delete every row of the month. To replace a month, upload its file again.")
    act = spec["action"]
    if not p["affected"] and act != "add_column":
        raise Invalid("No rows would change")
    b, u, rev = sql_list([batch_id]), sql_list([user]), _revision(batch_id)
    ch = ax_load.ch
    log = "INSERT INTO ub.edit_log (batch_id, line_no, action, column, old_value, new_value, user, revision) "
    if act == "add_column":  # the column exists from now on (for every month); its values wait like any edit
        col = ub_custom.add(spec["column"]["name"], spec["column"]["kind"], user, RESERVED)
        ident = f"x:{col['key']}"
        kind = ub_custom.KINDS[col["kind"]].split(" ")[0].lower()
        fill = f"; filled with {spec['sets'][0]['formula']}" if spec["sets"] else ""
        what = f"added the {kind} column {col['name']}{fill} ({p['affected']:,} rows)"
        ch(log + f"VALUES ({b}, 0, 'formula', {sql_list([ident])}, {sql_list([p['rule_text'][:900]])}, "
                 f"{sql_list([what])}, {u}, {rev})")
        if not spec["sets"] or not p["affected"]:
            return {"ok": True, "affected": 0, "action": act, "column": col}
        spec = check({"rule": spec["rule"], "action": "update",
                      "sets": [{"field": ident, "formula": spec["sets"][0]["formula"]}]})
        _write_update(b, u, rev, _plan(batch_id, spec), spec)
        return {"ok": True, "affected": p["affected"], "action": act, "column": col}
    plan = _plan(batch_id, spec)
    sets = "; ".join(f"{F.label(s['field'])} = {s['formula']}" for s in spec["sets"])
    summary = {"update": f"set {sets}", "copy": "copy rows" + (f"; set {sets}" if spec["sets"] else ""),
               "delete": "delete rows"}[act]
    what = f"{summary[:900]} ({p['affected']:,} rows)"
    ch(log + f"VALUES ({b}, 0, 'formula', '', {sql_list([p['rule_text'][:900]])}, {sql_list([what])}, {u}, {rev})")
    cols = ", ".join(COLS)
    if act == "update":
        _write_update(b, u, rev, plan, spec)
    elif act == "copy":
        base = int(q(f"SELECT greatest((SELECT max(line_no) FROM ub.raw_rows WHERE batch_id = {b}), "
                     f"(SELECT max(line_no) FROM ub.raw_pending WHERE batch_id = {b})) AS n")[0]["n"])
        numbered = f"SELECT {base} + row_number() OVER (ORDER BY line_no) AS new_line, * FROM ({plan})"
        ch(log + f"SELECT {b}, new_line, 'bulk_insert', '', '', concat(f_AMOUNT, ' (', f_CUSTID, ') copied from line ', "
                 f"toString(line_no)), {u}, {rev} FROM ({numbered})")
        ch(f"INSERT INTO ub.raw_pending (batch_id, line_no, action, {cols}, extra, edited_by) "
           f"SELECT {b}, new_line, 'insert', {', '.join(f'f_{c}' for c in COLS)}, f_extra, {u} FROM ({numbered})")
    else:  # delete
        ch(log + f"SELECT {b}, line_no, 'bulk_delete', '', concat(o_AMOUNT, ' (', o_CUSTID, ')'), '', {u}, {rev} FROM ({plan})")
        added = [int(r["line_no"]) for r in q(f"SELECT line_no FROM ({plan}) WHERE pending = 'insert'")]
        for i in range(0, len(added), 5000):  # rows added in this draft: just drop them
            ch(f"DELETE FROM ub.raw_pending WHERE batch_id = {b} AND line_no IN ({', '.join(map(str, added[i:i + 5000]))})")
        ch(f"INSERT INTO ub.raw_pending (batch_id, line_no, action, {cols}, extra, edited_by) "
           f"SELECT {b}, line_no, 'delete', {cols}, extra, {u} FROM ub.raw_rows WHERE batch_id = {b} "
           f"AND line_no IN (SELECT line_no FROM ({plan}) WHERE pending != 'insert')")
    return {"ok": True, "affected": p["affected"], "action": act}


def _write_update(b: str, u: str, rev: int, plan: str, spec: dict) -> None:
    """The changed cells into the log (first: it reads the rows before the change), then the rows."""
    ch = ax_load.ch
    log = "INSERT INTO ub.edit_log (batch_id, line_no, action, column, old_value, new_value, user, revision) "
    touched = {s["field"] for s in spec["sets"]} | {lbl for s in spec["sets"] for lbl in COMPANIONS.get(s["field"], [])}
    parts = [f"SELECT {b}, line_no, 'bulk_update', {sql_list([c])}, o_{alias(c)}, f_{alias(c)}, {u}, {rev} "
             f"FROM ({plan}) WHERE f_{alias(c)} != o_{alias(c)}" for c in COLS + [s["field"] for s in spec["sets"] if F.is_custom(s["field"])]
             if c in touched]
    if parts:
        ch(log + " UNION ALL ".join(parts))
    ch(f"INSERT INTO ub.raw_pending (batch_id, line_no, action, {', '.join(COLS)}, extra, edited_by) "
       f"SELECT {b}, line_no, if(pending = 'insert', 'insert', 'update'), {', '.join(f'f_{c}' for c in COLS)}, f_extra, {u} "
       f"FROM ({plan}) WHERE changed")


def remove_column(ident: str, user: str) -> dict:
    """Hide an added column everywhere. Its values stay in the rows (and in the change log)."""
    key = ident[2:] if ident.startswith("x:") else ident
    try:
        ub_custom.remove(key, user)
    except ValueError as e:
        raise Invalid(str(e))
    return {"ok": True}


# ---------------------------------------------------------------------------- saved formulas

def saved() -> list[dict]:
    rows = q("SELECT name, spec, saved_by, toString(saved_at) AS saved_at FROM ub.saved_formulas FINAL "
             "WHERE deleted = 0 ORDER BY name")
    return [{**r, "spec": json.loads(r["spec"])} for r in rows]


def save(name: str, spec: dict, user: str) -> dict:
    name = str(name or "").strip()
    if not 1 <= len(name) <= 80:
        raise Invalid("Give the formula a name (up to 80 characters)")
    if (spec or {}).get("action") == "add_column":
        raise Invalid("Adding a column is a one-off: it is not saved as a formula")
    spec = check(spec)
    ax_load.ch("INSERT INTO ub.saved_formulas (name, spec, saved_by) VALUES "
               f"({sql_list([name, json.dumps(spec), user])})")
    return {"ok": True}


def forget(name: str, user: str) -> dict:
    ax_load.ch("INSERT INTO ub.saved_formulas (name, spec, saved_by, deleted) VALUES "
               f"({sql_list([str(name), '{}', user])}, 1)")
    return {"ok": True}


def reference() -> dict:
    """What the builder offers: fields (with type), comparisons, functions, examples."""
    from review import DATES, NUMBER, WHOLE
    kind = lambda c: "number" if c in NUMBER | WHOLE else "code" if c in CODES else "date" if c in DATES else "text"
    added = [{"name": f"x:{c['key']}", "label": c["name"], "kind": c["kind"], "editable": True, "choices": [],
              "custom": True, "created_by": c["created_by"]} for c in ub_custom.columns()]
    return {
        "fields": [{"name": c, "label": LABELS.get(c, c), "kind": kind(c), "editable": c in EDITABLE,
                    "choices": list(CODES.get(c, []))} for c in COLS] + added,
        "kinds": ub_custom.KINDS,
        "ops": F.OPS, "functions": {k: v[2] for k, v in F.FUNCS.items()}, "actions": ACTIONS,
        "examples": [
            {"label": "Increase by 5%", "field": "AMOUNT", "formula": "ROUND([Amount] * 1.05, 2)"},
            {"label": "Recalculate from quantity × rate", "field": "AMOUNT", "formula": "ROUND([Quantity] * 2.85, 2)"},
            {"label": "Discount 10% on Praslin only", "field": "AMOUNT", "formula": "IF([Island] = 2, [Amount] * 0.9, [Amount])"},
            {"label": "Never below zero", "field": "AMOUNT", "formula": "MAX([Amount], 0)"},
            {"label": "Move to another tariff code", "field": "TARIFFGROUPCODE", "formula": "\"EMD1\""},
            {"label": "Tidy text", "field": "SECTORDESCRIPTION", "formula": "TRIM(UPPER([Sector]))"},
        ],
    }
