"""The formula builder's bulk edits: a rule picks rows of one batch, an action changes them.

    spec = {"rule":   {"combine": "and", "rules": [{"field": "REGION", "op": "eq", "value": "2"}, ...]},
            "action": "update" | "copy" | "delete",
            "sets":   [{"field": "AMOUNT", "formula": "[Amount] * 1.05"}, ...]}

update  sets the fields on every matching row
copy    adds a copy of every matching row, with the fields set
delete  deletes every matching row

Rows are matched as the reviewer sees them (pending edits included). Nothing reaches the
dashboards here: the result is written to ub.raw_pending like any edit and waits for "Finalize
modified data". The change log gets one 'formula' entry (the rule and the formulas) and one
bulk_* entry per changed cell. preview() shows what apply() would do.
"""
import json

import ax_load
import formula as F
from review import CODES, COLS, EDITABLE, LABELS, Invalid, _merged, q
from ub_load import sql_list

ACTIONS = {"update": "Change values", "copy": "Copy as new rows", "delete": "Delete rows"}
MAX_SETS = 20
MAX_COPY = 100_000
COMPANIONS = {"UTILITYTYPE": ["UTILITYTYPEDESCRIPTION"], "REGION": ["REGIONNAME"],
              "TARIFFGROUPCODE": ["TARIFFGROUPDESC", "STATCATEGORIES", "CATEGORYDESCRIPTION", "SECTORGROUP",
                                  "SECTORDESCRIPTION"]}


def check(spec: dict) -> dict:
    """A clean spec, or Invalid / FormulaError saying what is wrong."""
    if not isinstance(spec, dict):
        raise Invalid("Malformed request")
    action = spec.get("action")
    if action not in ACTIONS:
        raise Invalid("Pick what to do: change values, copy as new rows, or delete rows")
    sets = []
    if action != "delete":
        seen = set()
        for s in spec.get("sets") or []:
            col = F.field(str(s.get("field", "")))
            if col not in EDITABLE:
                raise Invalid(f"{LABELS.get(col, col)} cannot be changed")
            if col in seen:
                raise Invalid(f"{LABELS.get(col, col)} is set twice")
            seen.add(col)
            src = str(s.get("formula", "")).strip()
            try:
                F.value_sql(col, src)
            except F.FormulaError as e:
                e.args = (f"{LABELS.get(col, col)}: {e}",)
                e.field = col
                raise
            sets.append({"field": col, "formula": src})
        if action == "update" and not sets:
            raise Invalid("Add at least one field to change")
        if len(sets) > MAX_SETS:
            raise Invalid(f"At most {MAX_SETS} fields at a time")
    rule = spec.get("rule") or {"combine": "and", "rules": []}
    F.compile_rule(rule)
    return {"rule": rule, "action": action, "sets": sets}


def _plan(batch_id: str, spec: dict) -> str:
    """One row per matching row: its line, pending state, o_<col> (now), f_<col> (after), changed, bad_<i>."""
    cond = F.compile_rule(spec["rule"])
    targets = {s["field"]: s["formula"] for s in spec["sets"]}
    inner_cols = [f"m.line_no AS line_no", "m.pending AS pending"] + [f"m.{c} AS o_{c}" for c in COLS]
    inner_cols += [f"{F.value_sql(c, f)} AS n_{c}" for c, f in targets.items()]
    inner_cols += [f"({F.invalid_sql(c, f)}) AS bad_{i}" for i, (c, f) in enumerate(targets.items())]
    inner = (f"SELECT {', '.join(inner_cols)} FROM {_merged(batch_id)} AS m "
             f"WHERE m.pending != 'delete' AND ({cond})")
    final = {}
    for c in COLS:
        final[c] = f"n_{c}" if c in targets else f"o_{c}"
    for code, labels in COMPANIONS.items():  # a changed code brings its labels, unless they are set too
        if code in targets:
            for lbl in labels:
                if lbl not in targets:
                    lookup = (f"(SELECT (groupArray(k), groupArray(v)) FROM (SELECT {code} AS k, "
                              f"argMax({lbl}, (batch_id, line_no)) AS v FROM ub.raw_rows GROUP BY k))")
                    final[lbl] = (f"if(n_{code} != o_{code}, transform(n_{code}, tupleElement({lookup}, 1), "
                                  f"tupleElement({lookup}, 2), o_{lbl}), o_{lbl})")
    changed = " OR ".join(f"({final[c]} != o_{c})" for c in COLS if final[c] != f"o_{c}") or "0"
    outer = ", ".join(f"{final[c]} AS f_{c}" for c in COLS)
    bad = "".join(f", bad_{i}" for i in range(len(targets)))
    return (f"SELECT line_no, pending, {', '.join(f'o_{c}' for c in COLS)}, {outer}, ({changed}) AS changed{bad} "
            f"FROM ({inner})")


def preview(batch_id: str, spec: dict, sample: int = 20) -> dict:
    spec = check(spec)
    plan = _plan(batch_id, spec)
    targets = [s["field"] for s in spec["sets"]]
    bads = ", ".join(f"countIf(bad_{i}) AS bad_{i}" for i in range(len(targets)))
    st = q(f"SELECT count() AS matched, countIf(changed) AS changed, "
           f"sum(toFloat64OrZero(o_AMOUNT)) AS amount_before, sum(toFloat64OrZero(f_AMOUNT)) AS amount_after, "
           f"sum(toFloat64OrZero(o_QUANTITY)) AS quantity_before, sum(toFloat64OrZero(f_QUANTITY)) AS quantity_after"
           f"{', ' + bads if bads else ''} FROM ({plan})")[0]
    total = q(f"SELECT count() AS rows, sum(toFloat64OrZero(AMOUNT)) AS amount FROM {_merged(batch_id)} AS m "
              f"WHERE m.pending != 'delete'")[0]
    matched, before, after = int(st["matched"]), float(st["amount_before"]), float(st["amount_after"])
    act = spec["action"]
    affected = int(st["changed"]) if act == "update" else matched
    new_total = {"update": float(total["amount"]) - before + after, "delete": float(total["amount"]) - before,
                 "copy": float(total["amount"]) + after}[act]
    new_rows = {"update": int(total["rows"]), "delete": int(total["rows"]) - matched,
                "copy": int(total["rows"]) + matched}[act]
    invalid = [{"field": c, "label": LABELS.get(c, c), "rows": int(st[f"bad_{i}"])}
               for i, c in enumerate(targets) if int(st[f"bad_{i}"])]
    shown = list(dict.fromkeys(["CUSTID", "TARIFFGROUPDESC", "REGION"] + targets +
                               [lbl for c in targets for lbl in COMPANIONS.get(c, []) if lbl not in targets][:2] +
                               ["QUANTITY", "AMOUNT"]))
    where = "changed" if act == "update" else "1"
    got = q(f"SELECT line_no, pending, {', '.join(f'o_{c}, f_{c}' for c in shown)} FROM ({plan}) "
            f"WHERE {where} ORDER BY line_no LIMIT {int(sample)}")
    rows = [{"line_no": int(r["line_no"]), "pending": r["pending"],
             "before": {c: r[f"o_{c}"] for c in shown}, "after": {c: r[f"f_{c}"] for c in shown}} for r in got]
    return {"matched": matched, "affected": affected, "action": act, "rule_text": F.describe_rule(spec["rule"]),
            "amount_before": before, "amount_after": after,
            "quantity_before": float(st["quantity_before"]), "quantity_after": float(st["quantity_after"]),
            "batch_rows": int(total["rows"]), "batch_amount": float(total["amount"]),
            "batch_rows_after": new_rows, "batch_amount_after": new_total,
            "invalid": invalid, "columns": shown, "targets": targets, "sample": rows,
            "too_many": act == "copy" and matched > MAX_COPY}


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
    if not p["affected"]:
        raise Invalid("No rows would change")
    b, u, rev = sql_list([batch_id]), sql_list([user]), _revision(batch_id)
    plan = _plan(batch_id, spec)
    act = spec["action"]
    summary = {"update": "set " + "; ".join(f"{LABELS.get(s['field'], s['field'])} = {s['formula']}" for s in spec["sets"]),
               "copy": "copy rows" + ("; set " + "; ".join(f"{LABELS.get(s['field'], s['field'])} = {s['formula']}"
                                                          for s in spec["sets"]) if spec["sets"] else ""),
               "delete": "delete rows"}[act]
    ch = ax_load.ch
    log = "INSERT INTO ub.edit_log (batch_id, line_no, action, column, old_value, new_value, user, revision) "
    what = f"{summary[:900]} ({p['affected']:,} rows)"
    ch(log + f"VALUES ({b}, 0, 'formula', '', {sql_list([p['rule_text'][:900]])}, {sql_list([what])}, {u}, {rev})")
    cols = ", ".join(COLS)
    if act == "update":
        # the log first: it reads the rows as they are before this change
        changed_cols = [c for c in COLS if f"f_{c}" in plan and c in {s["field"] for s in spec["sets"]} |
                        {lbl for s in spec["sets"] for lbl in COMPANIONS.get(s["field"], [])}]
        ch(log + " UNION ALL ".join(
            f"SELECT {b}, line_no, 'bulk_update', '{c}', o_{c}, f_{c}, {u}, {rev} "
            f"FROM ({plan}) WHERE f_{c} != o_{c}" for c in changed_cols))
        ch(f"INSERT INTO ub.raw_pending (batch_id, line_no, action, {cols}, edited_by) "
           f"SELECT {b}, line_no, if(pending = 'insert', 'insert', 'update'), {', '.join(f'f_{c}' for c in COLS)}, {u} "
           f"FROM ({plan}) WHERE changed")
    elif act == "copy":
        base = int(q(f"SELECT greatest((SELECT max(line_no) FROM ub.raw_rows WHERE batch_id = {b}), "
                     f"(SELECT max(line_no) FROM ub.raw_pending WHERE batch_id = {b})) AS n")[0]["n"])
        numbered = f"SELECT {base} + row_number() OVER (ORDER BY line_no) AS new_line, * FROM ({plan})"
        ch(log + f"SELECT {b}, new_line, 'bulk_insert', '', '', concat(f_AMOUNT, ' (', f_CUSTID, ') copied from line ', "
                 f"toString(line_no)), {u}, {rev} FROM ({numbered})")
        ch(f"INSERT INTO ub.raw_pending (batch_id, line_no, action, {cols}, edited_by) "
           f"SELECT {b}, new_line, 'insert', {', '.join(f'f_{c}' for c in COLS)}, {u} FROM ({numbered})")
    else:  # delete
        ch(log + f"SELECT {b}, line_no, 'bulk_delete', '', concat(o_AMOUNT, ' (', o_CUSTID, ')'), '', {u}, {rev} FROM ({plan})")
        added = [int(r["line_no"]) for r in q(f"SELECT line_no FROM ({plan}) WHERE pending = 'insert'")]
        for i in range(0, len(added), 5000):  # rows added in this draft: just drop them
            ch(f"DELETE FROM ub.raw_pending WHERE batch_id = {b} AND line_no IN ({', '.join(map(str, added[i:i + 5000]))})")
        ch(f"INSERT INTO ub.raw_pending (batch_id, line_no, action, {cols}, edited_by) "
           f"SELECT {b}, line_no, 'delete', {cols}, {u} FROM ub.raw_rows WHERE batch_id = {b} "
           f"AND line_no IN (SELECT line_no FROM ({plan}) WHERE pending != 'insert')")
    return {"ok": True, "affected": p["affected"], "action": act}


# ---------------------------------------------------------------------------- saved formulas

def saved() -> list[dict]:
    rows = q("SELECT name, spec, saved_by, toString(saved_at) AS saved_at FROM ub.saved_formulas FINAL "
             "WHERE deleted = 0 ORDER BY name")
    return [{**r, "spec": json.loads(r["spec"])} for r in rows]


def save(name: str, spec: dict, user: str) -> dict:
    name = str(name or "").strip()
    if not 1 <= len(name) <= 80:
        raise Invalid("Give the formula a name (up to 80 characters)")
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
    return {
        "fields": [{"name": c, "label": LABELS.get(c, c), "kind": kind(c), "editable": c in EDITABLE,
                    "choices": list(CODES.get(c, []))} for c in COLS],
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
