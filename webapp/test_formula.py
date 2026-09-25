#!/usr/bin/env python3
"""Thorough tests of the formula builder, on a throwaway test month.

    python3 webapp/test_formula.py            # needs ClickHouse with ub loaded (it samples real rows)

Builds a test batch (5,000 real rows moved to March 2024, plus hand-made awkward rows),
runs every check against it, and removes it again -- the real months are never edited.
Each result is compared with an independent calculation in Python, not just "no error".
Prints a report; exits 1 if anything failed.
"""
import csv
import io
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]

import ax_load  # noqa: E402
import bulk  # noqa: E402
import formula as F  # noqa: E402
import review as R  # noqa: E402
import ub_load  # noqa: E402

ch = ax_load.ch
BATCH = "TEST-FORMULA-BUILDER"
PERIOD = "2024-03-01"
USER = "formula.test"
results = []  # (area, check, ok, detail)
timings = []


def check(area, name, ok, detail=""):
    results.append((area, name, bool(ok), str(detail)[:300]))
    if not ok:
        print(f"  FAIL [{area}] {name}: {detail}", flush=True)


def expect_error(area, name, fn, contains=""):
    try:
        fn()
    except (F.FormulaError, R.Invalid) as e:
        check(area, name, contains.lower() in str(e).lower(), f"error: {e}")
        return e
    check(area, name, False, "was accepted")


def timed(label, fn):
    t = time.time()
    out = fn()
    timings.append((label, time.time() - t))
    return out


# ---------------------------------------------------------------------------- the test month

SPECIAL = [  # awkward rows, on top of the real sample
    {"CUSTID": "O'BRIEN-1", "AMOUNT": "-125.5", "QUANTITY": "0", "TARIFFGROUPDESC": "Domestic 'quoted'"},
    {"CUSTID": 'QUOTE"2', "AMOUNT": "0", "QUANTITY": "3", "MCSEXTERNALASSETID": ""},
    {"CUSTID": "CÜS-ÜNÏ-3", "AMOUNT": "1e3", "QUANTITY": "2.5", "TARIFFGROUPDESC": "Tarif ünïcode"},
    {"CUSTID": "BACK\\SLASH-4", "AMOUNT": "10.005", "QUANTITY": "", "SECTORDESCRIPTION": "  padded  "},
    {"CUSTID": "BIG-5", "AMOUNT": "98765432.1234", "QUANTITY": "1000000"},
]


def make_batch() -> None:
    cols = ub_load.COLS
    rows = R.q(f"SELECT {', '.join(cols)} FROM ub.raw_rows ORDER BY cityHash64(batch_id, line_no) LIMIT 5000")
    base = dict(rows[0])
    for sp in SPECIAL:
        rows.append({**base, **sp})
    out = io.StringIO()
    w = csv.writer(out, delimiter="\t", lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        r = {**r, "BatchId": BATCH, "INVOICEDATE": "2024-03-15"}
        w.writerow([r[c] for c in cols])
    path = Path(tempfile.gettempdir()) / "formula-test-batch.tsv"
    path.write_text(out.getvalue(), encoding="utf-8")
    ub_load.load([str(path)])
    path.unlink()


def drop_batch() -> None:
    b = F.sql_text(BATCH)
    for t in ("raw_rows", "fact_lines"):
        ch(f"ALTER TABLE ub.{t} DROP PARTITION {b}")
    for t in ("raw_batches", "batch_events", "raw_pending", "edit_log", "build_log", "load_log"):
        ch(f"DELETE FROM ub.{t} WHERE batch_id = {b}")
    ch("DELETE FROM ub.saved_formulas WHERE startsWith(name, 'TEST ')")
    ch("DELETE FROM ub.custom_columns WHERE startsWith(name, 'TEST ')")  # only the columns tests added
    import ub_custom
    ub_custom.refresh_view()
    ax_load.run_sql_file(HERE.parent / "ub_aggregates.sql")


def rows_now() -> list[dict]:
    """The test batch as the reviewer sees it (pending edits included), for the Python oracle."""
    return R.q(f"SELECT * FROM {R._merged(BATCH)} AS m WHERE m.pending != 'delete' ORDER BY line_no "
               "SETTINGS prefer_column_name_to_alias = 1")


def num(v) -> float:  # how the builder reads a text field as a number: toFloat64OrZero
    try:
        x = float(str(v).strip()) if str(v).strip() not in ("", "inf", "-inf", "nan") else 0.0
        return x if math.isfinite(x) else 0.0
    except ValueError:
        return 0.0


def pending() -> int:
    return int(R.batch(BATCH)["pending"])


def discard():
    ch(f"DELETE FROM ub.raw_pending WHERE batch_id = {F.sql_text(BATCH)}")


# ---------------------------------------------------------------------------- 1. the formula language

def formulas(rows):
    area = "1 Formula language"
    # literals: exact answers
    lit = {"1 + 2 * 3": 7, "(1 + 2) * 3": 9, "10 / 4": 2.5, "-3 + 5": 2, "--3": 3, "2 * -3": -6, "1e3 + .5": 1000.5,
           "ROUND(2.345, 2)": 2.35, "ROUND(2.5)": 3, "ROUND(-2.345, 1)": -2.3, "ROUND(2.675, 2)": 2.68,
           "ROUND(-2.5)": -3, "ROUND(1.005, 2)": 1.01, "ROUND(0.125, 2)": 0.13, "ROUND(3.5)": 4, "ROUND(4.5)": 5, "ABS(-7.5)": 7.5, "MIN(4, 2, 9)": 2,
           "MAX(4, 2, 9)": 9, "IF(3 > 2, 10, 20)": 10, "IF(AND(1 < 2, 2 < 1), 1, 0)": 0, "IF(OR(1 < 2, 2 < 1), 1, 0)": 1,
           "IF(NOT(1 = 1), 1, 0)": 0, "LEN(\"héllo\")": 5, "NUMBER(\"12.5\") * 2": 25, "NUMBER(\"abc\")": 0,
           "IF(1 <> 2, 1, 0)": 1, "IF(1 != 2, 1, 0)": 1, "IF(2 >= 2, 1, 0)": 1, "IF(2 <= 1, 1, 0)": 0,
           "IF(1 = 1, IF(2 = 3, 5, 6), 7)": 6}
    for src, want in lit.items():
        sql, t, _ = F.compile_formula(src)
        got = float(R.q(f"SELECT {sql} AS v")[0]["v"])
        check(area, f"{src} = {want}", abs(got - want) < 1e-9, f"got {got}")
    txt = {'"a" & "b" & 1': "ab1", 'CONCAT("x", 2, "y")': "x2y", 'UPPER("ümlaut")': "ÜMLAUT", 'LOWER("ABC")': "abc",
           'TRIM("  pad  ")': "pad", 'LEFT("abcdef", 3)': "abc", 'RIGHT("abcdef", 2)': "ef",
           'REPLACE("a-b-c", "-", "+")': "a+b+c", '"it""s"': 'it"s', "'it''s'": "it's", 'TEXT(2.5)': "2.5",
           'IF(CONTAINS("Domestic W", "DOMESTIC"), "yes", "no")': "yes", 'IF(ISBLANK("  "), "blank", "no")': "blank",
           'MAX("apple", "pear")': "pear", 'IF(1 = 1, "a", 5)': "a", '"a\\\' OR 1=1 --"': "a\\' OR 1=1 --"}
    for src, want in txt.items():
        sql, t, _ = F.compile_formula(src)
        got = R.q(f"SELECT {sql} AS v")[0]["v"]
        check(area, f"{src} = {want!r}", got == want, f"got {got!r}")
    # per row, against Python, on every row of the test month
    per_row = [
        ("[Amount] * 1.05", lambda r: num(r["AMOUNT"]) * 1.05),
        ("ROUND([Amount] * 1.05, 2)", lambda r: round_half_away(num(r["AMOUNT"]) * 1.05, 2)),
        ("[Amount] + [Quantity]", lambda r: num(r["AMOUNT"]) + num(r["QUANTITY"])),
        ("IF([Island] = 2, [Amount] * 0.9, [Amount])", lambda r: num(r["AMOUNT"]) * (0.9 if num(r["REGION"]) == 2 else 1)),
        ("IF([Quantity] <= 100, [Quantity] * 2.5, IF([Quantity] <= 200, [Quantity] * 3.1, [Quantity] * 3.8))",
         lambda r: num(r["QUANTITY"]) * (2.5 if num(r["QUANTITY"]) <= 100 else 3.1 if num(r["QUANTITY"]) <= 200 else 3.8)),
        ("MAX([Amount], 0)", lambda r: max(num(r["AMOUNT"]), 0)),
        ("ABS([Amount]) / 2", lambda r: abs(num(r["AMOUNT"])) / 2),
        ("amount - QUANTITY", lambda r: num(r["AMOUNT"]) - num(r["QUANTITY"])),  # export names, any case
        ("[amount] * [ISLAND]", lambda r: num(r["AMOUNT"]) * num(r["REGION"])),  # labels, any case
    ]
    for src, fn in per_row:
        sql, _, _ = F.compile_formula(src)
        got = R.q(f"SELECT m.line_no AS n, {sql} AS v FROM {R._merged(BATCH)} AS m WHERE m.pending != 'delete' ORDER BY n")
        bad = [(g["n"], g["v"], fn(r)) for g, r in zip(got, rows) if abs(float(g["v"]) - fn(r)) > 1e-6 * max(1, abs(fn(r)))]
        check(area, f"{src} on all {len(rows):,} rows", len(got) == len(rows) and not bad, bad[:3])
    per_row_txt = [
        ('UPPER([Tariff group])', lambda r: r["TARIFFGROUPDESC"].upper()),
        ('"C:" & [Customer]', lambda r: "C:" + r["CUSTID"]),
        ('IF(ISBLANK([Meter]), "NO METER", [Meter])', lambda r: r["MCSEXTERNALASSETID"] if r["MCSEXTERNALASSETID"].strip() else "NO METER"),
        ('LEFT([Customer], 3)', lambda r: r["CUSTID"][:3]),
        ('TRIM([Sector])', lambda r: r["SECTORDESCRIPTION"].strip()),
        ('IF(CONTAINS([Tariff group], "domestic"), "D", "O")', lambda r: "D" if "domestic" in r["TARIFFGROUPDESC"].lower() else "O"),
    ]
    for src, fn in per_row_txt:
        sql, _, _ = F.compile_formula(src)
        got = R.q(f"SELECT {sql} AS v FROM {R._merged(BATCH)} AS m WHERE m.pending != 'delete' ORDER BY m.line_no")
        bad = [(g["v"], fn(r)) for g, r in zip(got, rows) if g["v"] != fn(r)]
        check(area, f"{src} on all {len(rows):,} rows", not bad, bad[:3])


def round_half_away(x, d):  # Excel's ROUND, independently: halves away from zero, on the number as written
    from decimal import ROUND_HALF_UP, Decimal
    return float(Decimal(repr(x)).quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------- 2. mistakes are caught

def mistakes():
    area = "2 Mistakes caught"
    cases = [("[Amount] *", "ends too early", 10), ("[Nope] + 1", "unknown field", 0), ("FOO(1)", "unknown function", 0),
             ("ROUND(1, 2, 3)", "takes", 0), ("ROUND([Amount], [Quantity])", "whole number", 0),
             ("ROUND(1, 12)", "between 0 and 8", 0), ("IF([Amount], 1, 2)", "condition", 0), ("(1 + 2", "expected ')'", 6),
             ("1 + 2)", "unexpected ')'", 5), ('"open', "unexpected character", 0), ("", "empty", 0),
             ("1,000", "unexpected ','", 1), ("[Amount] ^ 2", "unexpected character", 9), ("5%", "unexpected character", 1),
             ("x" * 700, "at most", None), ("IF(1, 2)", "takes", 0), ("MIN(1)", "takes", 0)]
    for src, msg, pos in cases:
        e = expect_error(area, f"{src[:30]!r} -> '{msg}'", lambda s=src: F.compile_formula(s), msg)
        if e is not None and pos is not None:
            check(area, f"{src[:30]!r} points at position {pos}", e.pos == pos, f"pos {e.pos}")
    expect_error(area, "a TRUE/FALSE formula cannot be a field's value", lambda: F.value_sql("AMOUNT", "[Amount] > 1"), "TRUE/FALSE")
    rule_cases = [({"rules": [{"field": "nope", "op": "eq", "value": 1}]}, "unknown field"),
                  ({"rules": [{"field": "AMOUNT", "op": "gt", "value": "abc"}]}, "needs a number"),
                  ({"rules": [{"field": "AMOUNT", "op": "between", "value": ["1"]}]}, "two values"),
                  ({"rules": [{"field": "AMOUNT", "op": "like", "value": "1"}]}, "unknown comparison"),
                  ({"rules": [{"field": "CUSTID", "op": "in", "value": ""}]}, "1 to 200"),
                  ({"rules": [{"formula": "[Amount] * 2"}]}, "TRUE or FALSE"),
                  ({"rules": [{"field": "AMOUNT", "op": "eq", "value": "x" * 300}]}, "200 characters"),
                  ({"rules": ["not a rule"]}, "malformed"),
                  ({"rules": [{"field": "AMOUNT", "op": "gt", "value": "1"}] * 61}, "at most 60"),
                  (nest(7), "nested too deeply")]
    for rule, msg in rule_cases:
        expect_error(area, f"rule -> '{msg}'", lambda r=rule: F.compile_rule(r), msg)
    for spec, msg in [({"action": "explode"}, "pick what to do"), ({"action": "update", "sets": []}, "at least one"),
                      ({"action": "update", "sets": [{"field": "BatchId", "formula": "1"}]}, "cannot be changed"),
                      ({"action": "update", "sets": [{"field": "AMOUNT", "formula": "1"}, {"field": "AMOUNT", "formula": "2"}]}, "twice"),
                      ({"action": "update", "sets": [{"field": "AMOUNT", "formula": "1"}] * 1 + [{"field": c, "formula": "1"} for c in R.EDITABLE[:21]]}, "")]:
        expect_error(area, f"spec -> '{msg or 'too many / duplicate fields'}'", lambda s=spec: bulk.check(s), msg)


def nest(depth):
    node = {"field": "AMOUNT", "op": "gt", "value": "1"}
    for _ in range(depth):
        node = {"combine": "and", "rules": [node]}
    return node


# ---------------------------------------------------------------------------- 3. hostile input

def hostile():
    area = "3 Hostile input"
    before = ch("SELECT count() FROM ub.raw_rows FORMAT TSV")
    formulas_ = ["1; DROP TABLE ub.raw_rows", "sleep(3)", "[Amount]) OR (1", "file('/etc/passwd')", "`x`",
                 "[Amount] -- comment", "1 /* x */", "url('http://x')", "system.tables", "\\' OR 1=1",
                 "[Amount]\x00", "remote('h', 'db')"]
    for src in formulas_:
        expect_error(area, f"formula {src[:28]!r} rejected", lambda s=src: F.compile_formula(s))
    values = ["x' OR '1'='1", "'; DROP TABLE ub.raw_rows; --", "\\", "\\'", "%' --", "é' OR 1=1 #", "x\x00y", "O'BRIEN-1", 'QUOTE"2']
    rows = rows_now()
    py = {"eq": lambda c, v: c.strip() == v.strip(), "contains": lambda c, v: v.lower() in c.lower(),
          "starts": lambda c, v: c.lower().startswith(v.lower()),
          "in": lambda c, v: c.strip() in [x.strip() for x in v.split(",") if x.strip()]}
    for v in values:
        for op in ("eq", "contains", "starts", "in"):
            w = F.compile_rule({"rules": [{"field": "CUSTID", "op": op, "value": v}]})
            n = int(R.q(f"SELECT count() AS n FROM ub.raw_rows WHERE batch_id = {F.sql_text(BATCH)} AND {w}")[0]["n"])
            want = sum(1 for r in rows if py[op](r["CUSTID"], v))
            check(area, f"value {v!r} with '{op}' is only ever a value: {want} row(s)", n == want, f"{n} rows")
    for evil_field in ["AMOUNT) OR (1", "system.tables", "AMOUNT; DROP"]:
        expect_error(area, f"field name {evil_field!r} rejected", lambda f=evil_field: F.compile_rule({"rules": [{"field": f, "op": "eq", "value": 1}]}))
    long_in = ",".join(f"C{i}" for i in range(201))
    expect_error(area, "an IN list of 201 values is refused", lambda: F.compile_rule({"rules": [{"field": "CUSTID", "op": "in", "value": long_in}]}), "1 to 200")
    after = ch("SELECT count() FROM ub.raw_rows FORMAT TSV")
    check(area, "the data is untouched afterwards", before == after, f"{before} -> {after}")


# ---------------------------------------------------------------------------- 4. conditions (the query builder)

def conditions(rows):
    area = "4 Conditions"
    t = lambda v: str(v).strip()
    cases = [
        ({"field": "REGION", "op": "eq", "value": "2"}, lambda r: num(r["REGION"]) == 2),
        ({"field": "REGION", "op": "ne", "value": "1"}, lambda r: num(r["REGION"]) != 1),
        ({"field": "REGION", "op": "in", "value": "2, 3"}, lambda r: t(r["REGION"]) in ("2", "3")),
        ({"field": "AMOUNT", "op": "gt", "value": "1000"}, lambda r: num(r["AMOUNT"]) > 1000),
        ({"field": "AMOUNT", "op": "ge", "value": "0"}, lambda r: num(r["AMOUNT"]) >= 0),
        ({"field": "AMOUNT", "op": "lt", "value": "-0.5"}, lambda r: num(r["AMOUNT"]) < -0.5),
        ({"field": "AMOUNT", "op": "le", "value": "10.005"}, lambda r: num(r["AMOUNT"]) <= 10.005),
        ({"field": "AMOUNT", "op": "between", "value": ["100", "500"]}, lambda r: 100 <= num(r["AMOUNT"]) <= 500),
        ({"field": "QUANTITY", "op": "empty"}, lambda r: not t(r["QUANTITY"])),
        ({"field": "MCSEXTERNALASSETID", "op": "empty"}, lambda r: not t(r["MCSEXTERNALASSETID"])),
        ({"field": "MCSEXTERNALASSETID", "op": "not_empty"}, lambda r: bool(t(r["MCSEXTERNALASSETID"]))),
        ({"field": "TARIFFGROUPDESC", "op": "contains", "value": "domestic"}, lambda r: "domestic" in r["TARIFFGROUPDESC"].lower()),
        ({"field": "TARIFFGROUPDESC", "op": "not_contains", "value": "domestic"}, lambda r: "domestic" not in r["TARIFFGROUPDESC"].lower()),
        ({"field": "CUSTID", "op": "starts", "value": "cus-00"}, lambda r: r["CUSTID"].lower().startswith("cus-00")),
        ({"field": "CUSTID", "op": "ends", "value": "7"}, lambda r: r["CUSTID"].lower().endswith("7")),
        ({"field": "CUSTID", "op": "eq", "value": "O'BRIEN-1"}, lambda r: t(r["CUSTID"]) == "O'BRIEN-1"),
        ({"field": "CUSTID", "op": "eq", "value": 'QUOTE"2'}, lambda r: t(r["CUSTID"]) == 'QUOTE"2'),
        ({"field": "CUSTID", "op": "eq", "value": "BACK\\SLASH-4"}, lambda r: t(r["CUSTID"]) == "BACK\\SLASH-4"),
        ({"field": "CUSTID", "op": "contains", "value": "üNï"}, lambda r: "ünï" in r["CUSTID"].lower()),
        ({"field": "SECTORDESCRIPTION", "op": "eq", "value": "padded"}, lambda r: t(r["SECTORDESCRIPTION"]) == "padded"),
        ({"field": "INVOICEDATE", "op": "between", "value": ["2024-03-01", "2024-03-31"]}, lambda r: "2024-03-01" <= r["INVOICEDATE"] <= "2024-03-31"),
        ({"formula": "[Amount] > [Quantity] * 5"}, lambda r: num(r["AMOUNT"]) > num(r["QUANTITY"]) * 5),
        ({"combine": "and", "rules": [{"field": "REGION", "op": "eq", "value": "1"},
                                      {"combine": "or", "rules": [{"field": "AMOUNT", "op": "gt", "value": "1000"},
                                                                  {"field": "TARIFFGROUPDESC", "op": "contains", "value": "commercial"}]}]},
         lambda r: num(r["REGION"]) == 1 and (num(r["AMOUNT"]) > 1000 or "commercial" in r["TARIFFGROUPDESC"].lower())),
        ({"combine": "or", "rules": [{"field": "REGION", "op": "eq", "value": "3"}, {"field": "UTILITYTYPE", "op": "eq", "value": "2"}]},
         lambda r: num(r["REGION"]) == 3 or num(r["UTILITYTYPE"]) == 2),
        ({"combine": "and", "negate": True, "rules": [{"field": "REGION", "op": "eq", "value": "1"}]}, lambda r: num(r["REGION"]) != 1),
        ({"combine": "and", "rules": []}, lambda r: True),
        ({"combine": "and", "rules": [{"combine": "or", "rules": []}]}, lambda r: True),
    ]
    for rule, fn in cases:
        node = rule if "rules" in rule else {"combine": "and", "rules": [rule]}
        want = sum(1 for r in rows if fn(r))
        p = bulk.preview(BATCH, {"rule": node, "action": "delete"})
        check(area, f"{F.describe_rule(node)[:70]} -> {want:,} rows", p["matched"] == want, f"builder says {p['matched']:,}")
    listed = R.rows(BATCH, rule={"combine": "and", "rules": [{"field": "REGION", "op": "eq", "value": "2"}]}, size=10)["total"]
    check(area, "Show matching rows lists the same rows as the preview",
          listed == sum(1 for r in rows if num(r["REGION"]) == 2), listed)


# ---------------------------------------------------------------------------- 5. actions

def merged_total():
    r = R.q(f"SELECT count() AS n, sum(toFloat64OrZero(AMOUNT)) AS a FROM {R._merged(BATCH)} AS m WHERE m.pending != 'delete'")[0]
    return int(r["n"]), float(r["a"])


def actions():
    area = "5 Actions"
    discard()
    rows = rows_now()
    n0, a0 = merged_total()
    praslin = {"combine": "and", "rules": [{"field": "REGION", "op": "eq", "value": "2"}]}
    spec = {"rule": praslin, "action": "update", "sets": [{"field": "AMOUNT", "formula": "ROUND([Amount] * 1.1, 2)"}]}
    p = bulk.preview(BATCH, spec)
    want_changed = sum(1 for r in rows if num(r["REGION"]) == 2 and round_str(num(r["AMOUNT"]) * 1.1) != r["AMOUNT"])
    check(area, "update: preview counts the rows that really change (unchanged values, e.g. 0, are skipped)",
          p["affected"] == want_changed, f"preview {p['affected']}, python {want_changed}")
    bulk.apply(BATCH, spec, USER)
    n1, a1 = merged_total()
    check(area, "update: the period total after apply equals the preview's prediction", abs(a1 - p["batch_amount_after"]) < 0.01,
          f"{a1:,.4f} vs {p['batch_amount_after']:,.4f}")
    check(area, "update: row count unchanged", n1 == n0, f"{n0} -> {n1}")
    check(area, "update: every changed row is a pending edit", pending() == p["affected"], f"{pending()} vs {p['affected']}")
    fact = float(R.q(f"SELECT sum(amount) AS a FROM ub.fact_lines WHERE batch_id = {F.sql_text(BATCH)}")[0]["a"])
    check(area, "update: the dashboards are unchanged until Finalize", abs(fact - a0) < 0.01, f"{fact} vs {a0}")
    # a second formula reads the first one's results (pending edits included)
    p2 = bulk.preview(BATCH, {"rule": praslin, "action": "update", "sets": [{"field": "AMOUNT", "formula": "[Amount] - [Amount] / 11"}]})
    check(area, "a formula reads earlier pending edits (x1.1 then -1/11 ≈ back to the original)",
          abs(p2["batch_amount_after"] - a0) < 0.02 * max(1, p["affected"]), f"{p2['batch_amount_after']:,.2f} vs {a0:,.2f}")
    # hand edits and formulas mix; undo of one row
    first = next(r for r in rows_now() if num(r["REGION"]) == 2)
    R.undo(BATCH, int(first["line_no"]), USER)
    check(area, "undo one row after a formula", pending() == p["affected"] - 1, pending())
    discard()
    check(area, "discard removes every pending edit", pending() == 0 and merged_total()[1] == a0)

    # companions: a changed code brings its labels
    p = bulk.preview(BATCH, {"rule": praslin, "action": "update", "sets": [{"field": "REGION", "formula": "3"}]})
    s0 = p["sample"][0]
    check(area, "a new Island code brings its island name", s0["after"].get("REGION") == "3" and "DIGUE" in str(
        R.q(f"SELECT f_REGIONNAME AS x FROM ({bulk._plan(BATCH, bulk.check({'rule': praslin, 'action': 'update', 'sets': [{'field': 'REGION', 'formula': '3'}]}))}) LIMIT 1")[0]["x"]).upper(),
        s0)
    plan = bulk._plan(BATCH, bulk.check({"rule": praslin, "action": "update",
                                         "sets": [{"field": "TARIFFGROUPCODE", "formula": '"EMD1"'}]}))
    lab = R.q(f"SELECT DISTINCT f_TARIFFGROUPDESC AS d, f_SECTORDESCRIPTION AS s FROM ({plan})")
    ref = R.q("SELECT argMax(TARIFFGROUPDESC, (batch_id, line_no)) AS d FROM ub.raw_rows WHERE TARIFFGROUPCODE = 'EMD1'")[0]["d"]
    check(area, "a new tariff code brings its tariff group and sector", len(lab) == 1 and lab[0]["d"] == ref, lab)
    plan = bulk._plan(BATCH, bulk.check({"rule": praslin, "action": "update",
                                         "sets": [{"field": "TARIFFGROUPCODE", "formula": '"BRAND-NEW"'}]}))
    kept = R.q(f"SELECT countIf(f_TARIFFGROUPDESC != o_TARIFFGROUPDESC) AS n FROM ({plan})")[0]["n"]
    check(area, "a code no row has yet keeps the old labels (set them too)", int(kept) == 0, kept)
    both = bulk.preview(BATCH, {"rule": praslin, "action": "update", "sets": [
        {"field": "TARIFFGROUPCODE", "formula": '"NEW1"'}, {"field": "TARIFFGROUPDESC", "formula": '"New group"'}]})
    check(area, "setting a code and its label together uses the label given", both["affected"] > 0)

    # values a field cannot hold block the apply
    for field_, src, why in [("REGION", "7", "island 7"), ("UTILITYTYPE", "0", "utility 0"), ("AMOUNT", '"abc"', "text in Amount"),
                             ("AMOUNT", "1/0", "division by zero"), ("QUANTITY", "0/0", "0/0"),
                             ("INVOICEDATE", '"15/03/2024"', "a d/m/y date"), ("INVOICEDATE", '"2024-13"', "half a date"),
                             ("ADDITIONALITEMSTYPE", "1000", "item type 1000"), ("AMOUNT", "1e30", "an amount too big to store")]:
        s = {"rule": praslin, "action": "update", "sets": [{"field": field_, "formula": src}]}
        pv = bulk.preview(BATCH, s)
        blocked = bool(pv["invalid"])
        try:
            bulk.apply(BATCH, s, USER)
            applied = True
        except R.Invalid:
            applied = False
        check(area, f"blocked: {why}", blocked and not applied, f"invalid={pv['invalid']} applied={applied}")
        discard()
    for field_, src in [("INVOICEDATE", '"2024-03-20"'), ("INVOICEDATE", '""'), ("QUANTITY", '"12.5"'), ("REGION", '"3"')]:
        pv = bulk.preview(BATCH, {"rule": praslin, "action": "update", "sets": [{"field": field_, "formula": src}]})
        check(area, f"allowed: {field_} = {src}", not pv["invalid"], pv["invalid"])
    expect_error(area, "nothing to change -> refused", lambda: bulk.apply(
        BATCH, {"rule": {"rules": [{"field": "CUSTID", "op": "eq", "value": "nobody"}]}, "action": "delete"}, USER), "no rows")

    # copy
    big = {"combine": "and", "rules": [{"field": "AMOUNT", "op": "gt", "value": "2000"}]}
    want = sum(1 for r in rows_now() if num(r["AMOUNT"]) > 2000)
    pc = bulk.preview(BATCH, {"rule": big, "action": "copy", "sets": [{"field": "CUSTID", "formula": '"COPY-" & [Customer]'}]})
    bulk.apply(BATCH, {"rule": big, "action": "copy", "sets": [{"field": "CUSTID", "formula": '"COPY-" & [Customer]'}]}, USER)
    n2, a2 = merged_total()
    copies = R.q(f"SELECT line_no, CUSTID FROM ub.raw_pending FINAL WHERE batch_id = {F.sql_text(BATCH)} AND action = 'insert' ORDER BY line_no")
    maxline = int(R.q(f"SELECT max(line_no) AS m FROM ub.raw_rows WHERE batch_id = {F.sql_text(BATCH)}")[0]["m"])
    check(area, "copy: one new row per matching row", pc["affected"] == want == len(copies) and n2 == n0 + want, f"{want} {len(copies)}")
    check(area, "copy: new rows get new line numbers after the last one",
          [int(c["line_no"]) for c in copies] == list(range(maxline + 1, maxline + 1 + want)))
    check(area, "copy: fields set on the copies", all(c["CUSTID"].startswith("COPY-") for c in copies))
    check(area, "copy: total grows by the copies' amount", abs(a2 - (a0 + pc["amount_after"])) < 0.01, f"{a2} vs {a0 + pc['amount_after']}")
    # delete: removes copies (they were only pending) and real rows
    pd = bulk.preview(BATCH, {"rule": {"rules": [{"field": "CUSTID", "op": "starts", "value": "COPY-"}]}, "action": "delete"})
    bulk.apply(BATCH, {"rule": {"rules": [{"field": "CUSTID", "op": "starts", "value": "COPY-"}]}, "action": "delete"}, USER)
    check(area, "delete: deleting pending copies simply drops them", pending() == 0 and merged_total() == (n0, a0) and pd["affected"] == want,
          (pending(), merged_total()))
    neg = {"combine": "and", "rules": [{"field": "AMOUNT", "op": "lt", "value": "0"}]}
    wneg = [r for r in rows_now() if num(r["AMOUNT"]) < 0]
    bulk.apply(BATCH, {"rule": neg, "action": "delete"}, USER)
    n3, a3 = merged_total()
    check(area, "delete: real rows marked deleted, total drops by their amount",
          n3 == n0 - len(wneg) and abs(a3 - (a0 - sum(num(r["AMOUNT"]) for r in wneg))) < 0.01, (n3, a3))
    again = bulk.preview(BATCH, {"rule": neg, "action": "delete"})
    check(area, "delete: deleted rows no longer match a rule", again["matched"] == 0, again["matched"])
    discard()
    # the copy limit
    old = bulk.MAX_COPY
    bulk.MAX_COPY = 10
    pv = bulk.preview(BATCH, {"rule": big, "action": "copy", "sets": []})
    expect_error(area, "copying more rows than the limit is refused", lambda: bulk.apply(BATCH, {"rule": big, "action": "copy", "sets": []}, USER), "at most")
    check(area, "the preview warns about the copy limit", pv["too_many"])
    bulk.MAX_COPY = old
    discard()


def round_str(x):
    """What an amount field holds after ROUND(x, 2): the shortest text, like the export ('26.2', '26')."""
    s = repr(round_half_away(x, 2))
    return s[:-2] if s.endswith(".0") else s


# ---------------------------------------------------------------------------- 6. end to end: finalize, dashboards, history, saved

def end_to_end():
    area = "6 End to end"
    discard()
    b = F.sql_text(BATCH)
    status0, rev0 = ub_load.status_of(BATCH)
    fact0 = float(R.q(f"SELECT sum(amount) AS a FROM ub.fact_lines WHERE batch_id = {b}")[0]["a"])
    mahe = {"combine": "and", "rules": [{"field": "REGION", "op": "eq", "value": "1"}, {"field": "UTILITYTYPE", "op": "eq", "value": "1"}]}
    spec = {"rule": mahe, "action": "update", "sets": [{"field": "AMOUNT", "formula": "ROUND([Amount] * 1.05, 2)"},
                                                       {"field": "SECTORDESCRIPTION", "formula": "TRIM([Sector])"}]}
    p = bulk.preview(BATCH, spec)
    since = R.q("SELECT toString(now64(3)) AS t")[0]["t"]
    bulk.apply(BATCH, spec, USER)
    timed("finalize (5,005 rows)", lambda: ub_load.apply(BATCH, USER))
    fact1 = float(R.q(f"SELECT sum(amount) AS a FROM ub.fact_lines WHERE batch_id = {b}")[0]["a"])
    check(area, "after Finalize the dashboard total equals the preview's prediction", abs(fact1 - p["batch_amount_after"]) < 0.01,
          f"{fact1:,.4f} vs {p['batch_amount_after']:,.4f}")
    pbi = float(R.q(f"SELECT sum(amount) AS a FROM ub.pbi_billing WHERE period = '{PERIOD}'")[0]["a"])
    check(area, "the Power BI tables are rebuilt too", abs(pbi - fact1) < 0.01, f"{pbi} vs {fact1}")
    st, rev = ub_load.status_of(BATCH)
    check(area, "Finalize makes the next revision, still a Draft", st == "draft" and rev == rev0 + 1, (st, rev))
    check(area, "no pending edits left after Finalize", pending() == 0)
    hist = [h for h in R.history(BATCH, 50) if h["ts"] >= since]
    f = [h for h in hist if h["action"] == "formula"]
    check(area, "history: one readable line per formula", len(f) == 1 and "Amount = ROUND([Amount] * 1.05, 2)" in f[0]["new_value"]
          and "Island is 1" in f[0]["old_value"], f[:1])
    check(area, "history: the formula line is not buried under cell entries", len(hist) <= 3, [h["action"] for h in hist])
    cells = R.q(f"SELECT old_value, new_value FROM ub.edit_log WHERE batch_id = {b} AND action = 'bulk_update' "
                f"AND column = 'AMOUNT' AND ts >= '{since}'")
    check(area, "history: every changed amount kept with its old value", len(cells) == p["affected"] or len(cells) <= p["affected"],
          f"{len(cells)} cells for {p['affected']} rows")
    wrong = [c for c in cells if round_str(num(c["old_value"]) * 1.05) != c["new_value"]]
    check(area, f"history: all {len(cells):,} old -> new amounts are exactly ROUND(old * 1.05, 2)", not wrong, wrong[:3])
    R.reviewed(BATCH, USER)
    check(area, "Mark as reviewed makes it Final", ub_load.status_of(BATCH)[0] == "final")
    # back to the uploaded amounts: a formula can undo a formula
    back = {"rule": mahe, "action": "update", "sets": [{"field": "AMOUNT", "formula": "ROUND([Amount] / 1.05, 2)"}]}
    bulk.apply(BATCH, back, USER)
    check(area, "editing Final data starts a Draft again (pending edits shown)", pending() > 0)
    ub_load.apply(BATCH, USER)
    fact2 = float(R.q(f"SELECT sum(amount) AS a FROM ub.fact_lines WHERE batch_id = {b}")[0]["a"])
    check(area, "reversing formula brings the total back (within rounding)", abs(fact2 - fact0) < 0.01 * max(1, p["affected"]),
          f"{fact2:,.2f} vs {fact0:,.2f}")
    # dates move rows between months
    one_row = {"rules": [{"field": "CUSTID", "op": "eq", "value": "BIG-5"}]}
    bulk.apply(BATCH, {"rule": one_row, "action": "update", "sets": [{"field": "INVOICEDATE", "formula": '"2024-04-02"'}]}, USER)
    ub_load.apply(BATCH, USER)
    periods = [r["p"] for r in R.q(f"SELECT DISTINCT toString(period) AS p FROM ub.fact_lines WHERE batch_id = {b} ORDER BY p")]
    check(area, "changing Invoice date moves that row to its new month on the dashboards", periods == ["2024-03-01", "2024-04-01"], periods)
    bulk.apply(BATCH, {"rule": one_row, "action": "update", "sets": [{"field": "INVOICEDATE", "formula": '"2024-03-15"'}]}, USER)
    ub_load.apply(BATCH, USER)
    # deleting every row of a month
    everything = {"combine": "and", "rules": []}
    try:
        bulk.apply(BATCH, {"rule": everything, "action": "delete"}, USER)
        refused = False
    except R.Invalid:
        refused = True
    check(area, "deleting every row of a month is refused (the month would vanish from review)", refused)
    discard()
    # saved formulas
    bulk.save("TEST Mahe electricity +5%", spec, USER)
    got = [s for s in bulk.saved() if s["name"] == "TEST Mahe electricity +5%"]
    check(area, "save and load a formula: the same rule, action and formulas come back", got and got[0]["spec"] == bulk.check(spec))
    bulk.save("TEST Mahe electricity +5%", {**spec, "action": "delete", "sets": []}, USER)
    got = [s for s in bulk.saved() if s["name"] == "TEST Mahe electricity +5%"]
    check(area, "saving again under the same name replaces it", len(got) == 1 and got[0]["spec"]["action"] == "delete")
    bulk.forget("TEST Mahe electricity +5%", USER)
    check(area, "delete a saved formula", not [s for s in bulk.saved() if s["name"] == "TEST Mahe electricity +5%"])
    expect_error(area, "a saved formula needs a name", lambda: bulk.save("  ", spec, USER), "name")


# ---------------------------------------------------------------------------- 7. roles and the API

def api():
    area = "7 Roles and API"
    os.environ["APP_DB"] = "puc_app_ftest"
    os.environ["APP_DEFAULT_PASSWORD"] = "Test-pass-1"
    import server
    from fastapi.testclient import TestClient
    server.ch("DROP DATABASE IF EXISTS puc_app_ftest")

    def login(u):
        c = TestClient(server.app)
        c.__enter__()
        assert c.post("/api/login", json={"username": u, "password": "Test-pass-1"}).status_code == 200
        return c
    ceo, op, rv = login("ceo"), login("operator"), login("reviewer")
    spec = {"rule": {"rules": [{"field": "REGION", "op": "eq", "value": "2"}]}, "action": "update",
            "sets": [{"field": "AMOUNT", "formula": "[Amount] * 2"}]}
    for who, c in (("executive", ceo), ("data operator", op)):
        codes = {c.post(f"/api/review/{BATCH}/formula/preview", json=spec).status_code,
                 c.post(f"/api/review/{BATCH}/formula/apply", json=spec).status_code,
                 c.get("/api/review/formula/reference").status_code, c.get("/api/review/formulas/saved").status_code,
                 c.post("/api/review/formulas/save", json={"name": "x", "spec": spec}).status_code}
        check(area, f"{who} cannot use the formula builder (403 everywhere)", codes == {403}, codes)
    anon = TestClient(server.app)
    anon.__enter__()
    check(area, "signed out: 401", anon.post(f"/api/review/{BATCH}/formula/preview", json=spec).status_code == 401)
    r = rv.post(f"/api/review/{BATCH}/formula/preview", json={**spec, "sets": [{"field": "AMOUNT", "formula": "[Amount] *"}]})
    check(area, "a formula mistake comes back as 400 with the field and position",
          r.status_code == 400 and r.json().get("field") == "AMOUNT" and r.json().get("pos") == 10, r.text)
    check(area, "a malformed request is a 400, not a crash",
          rv.post(f"/api/review/{BATCH}/formula/preview", json={"rule": "x", "action": "update"}).status_code == 400)
    check(area, "a non-JSON rule filter is a 400", rv.get(f"/api/review/{BATCH}/rows", params={"rule": "{bad"}).status_code == 400)
    server.JOB_LOCK.acquire()
    try:
        code = rv.post(f"/api/review/{BATCH}/formula/apply", json=spec).status_code
        check(area, "apply waits while a load or finalize runs (409)", code == 409, code)
        check(area, "preview still works while a job runs", rv.post(f"/api/review/{BATCH}/formula/preview", json=spec).status_code == 200)
    finally:
        server.JOB_LOCK.release()
    ok = rv.post(f"/api/review/{BATCH}/formula/apply", json=spec)
    check(area, "reviewer can apply through the API", ok.status_code == 200 and ok.json()["affected"] > 0, ok.text[:200])
    audit = server.q("SELECT count() AS n FROM puc_app_ftest.audit WHERE action = 'formula'")[0]["n"]
    check(area, "applying is written to the audit log", int(audit) >= 1, audit)
    discard()
    server.ch("DROP DATABASE puc_app_ftest")


# ---------------------------------------------------------------------------- 8. speed on a full month

def added_columns():
    area = "9 Added columns"
    import dashboards
    import ub_custom
    discard()
    b = F.sql_text(BATCH)
    rows = rows_now()
    praslin = {"combine": "and", "rules": [{"field": "REGION", "op": "eq", "value": "2"}]}
    add = {"action": "add_column", "column": {"name": "TEST Discount", "kind": "number"}, "rule": praslin,
           "formula": "ROUND([Amount] * 0.1, 2)"}
    want = {int(r["line_no"]): round_str(num(r["AMOUNT"]) * 0.1) for r in rows if num(r["REGION"]) == 2}
    p = bulk.preview(BATCH, add)
    check(area, "preview: the formula fills exactly the rows the rule picks", p["affected"] == len(want), (p["affected"], len(want)))
    check(area, "preview: amounts do not change", p["batch_amount_after"] == p["batch_amount"])
    out = bulk.apply(BATCH, add, USER)
    ident = f"x:{out['column']['key']}"
    check(area, "the column exists at once, for every month", any(c["name"] == "TEST Discount" for c in ub_custom.columns()))
    got = {int(r["line_no"]): (r["extra"] or {}).get(out["column"]["key"], "") for r in rows_now()}
    wrong = [(n, got.get(n), v) for n, v in want.items() if got.get(n) != v]
    blank = sum(1 for n, v in got.items() if n not in want and v != "")
    check(area, f"every one of the {len(want)} values is ROUND(amount * 0.1, 2)", not wrong, wrong[:3])
    check(area, "rows outside the rule stay blank", blank == 0, blank)
    check(area, "the values wait as pending edits", pending() == len(want), pending())
    fact_before = R.q(f"SELECT countIf(notEmpty(extra)) AS n FROM ub.fact_lines WHERE batch_id = {b}")[0]["n"]
    check(area, "not on the dashboards before Finalize", int(fact_before) == 0, fact_before)
    # used like any field
    big = sum(1 for v in want.values() if num(v) > 50)
    pr = bulk.preview(BATCH, {"rule": {"rules": [{"field": ident, "op": "gt", "value": "50"}]}, "action": "delete"})
    check(area, "a rule on the added column", pr["matched"] == big, (pr["matched"], big))
    pr = bulk.preview(BATCH, {"rule": {"rules": [{"field": ident, "op": "empty"}]}, "action": "delete"})
    check(area, "'is empty' finds the rows without a value", pr["matched"] == len(rows) - len(want), pr["matched"])
    F.load_custom()
    sql, _, _ = F.compile_formula("[Amount] - [TEST Discount]")
    vals = R.q(f"SELECT m.line_no AS n, {sql} AS v FROM {R._merged(BATCH)} AS m WHERE m.pending != 'delete' ORDER BY n")
    bad = [v for v in vals if abs(float(v["v"]) - (num(next(r for r in rows if int(r['line_no']) == int(v['n']))["AMOUNT"])
                                                   - num(want.get(int(v["n"]), "")))) > 1e-6]
    check(area, "a formula can use the added column (blank reads as 0)", not bad, bad[:2])
    # set it with a formula, hand edits, validation
    bulk.apply(BATCH, {"rule": {"rules": [{"field": ident, "op": "empty"}]}, "action": "update",
                       "sets": [{"field": ident, "formula": "0"}]}, USER)
    check(area, "Change values can set an added column", bulk.preview(BATCH, {"rule": {"rules": [{"field": ident, "op": "empty"}]},
                                                                          "action": "delete"})["matched"] == 0)
    inv = bulk.preview(BATCH, {"rule": praslin, "action": "update", "sets": [{"field": ident, "formula": '"abc"'}]})
    check(area, "text in a number column is blocked", bool(inv["invalid"]), inv["invalid"])
    ok_blank = bulk.preview(BATCH, {"rule": praslin, "action": "update", "sets": [{"field": ident, "formula": '""'}]})
    check(area, "a blank value is allowed in a number column", not ok_blank["invalid"], ok_blank["invalid"])
    one = next(iter(want))
    R.edit(BATCH, one, {ident: "12.5"}, USER)
    check(area, "hand edit of an added column", (rows_by_line(one)["extra"] or {}).get(out["column"]["key"]) == "12.5")
    expect_error(area, "hand edit checked against the type", lambda: R.edit(BATCH, one, {ident: "twelve"}, USER), "must be a number")
    # names
    for name, msg in [("Amount", "already a column"), ("TEST Discount", "already a column"), ("tariff group", "already a column"),
                      ("1abc", "starts with a letter"), ("x" * 41, "starts with a letter"), ("a]b", "starts with a letter"),
                      ("a'); DROP TABLE x; --", "starts with a letter")]:
        expect_error(area, f"column name {name[:20]!r} refused", lambda n=name: bulk.check(
            {"action": "add_column", "column": {"name": n, "kind": "text"}, "rule": praslin}), msg)
    expect_error(area, "a type other than number/text/date is refused", lambda: bulk.check(
        {"action": "add_column", "column": {"name": "TEST X", "kind": "blob"}}), "type")
    # a text column filled for every row, using the first column
    t = bulk.apply(BATCH, {"action": "add_column", "column": {"name": "TEST Band", "kind": "text"}, "rule": {"rules": []},
                           "formula": 'IF([TEST Discount] > 50, "Big", IF([Amount] > 1000, "High", "Normal"))'}, USER)
    check(area, "a text column filled on every row", t["affected"] == len(rows), t["affected"])
    empty = bulk.apply(BATCH, {"action": "add_column", "column": {"name": "TEST Empty", "kind": "date"}, "rule": {"rules": []}}, USER)
    check(area, "a column can start empty", empty["affected"] == 0 and any(c["name"] == "TEST Empty" for c in ub_custom.columns()))
    # finalize: ClickHouse and the dashboards
    ub_load.apply(BATCH, USER)
    rows = rows_now()
    key, tkey = out["column"]["key"], t["column"]["key"]
    py_sum = sum(num((r["extra"] or {}).get(key, "")) for r in rows)
    ch_sum = float(R.q(f"SELECT sum(`TEST Discount`) AS s FROM ub.v_custom WHERE batch_id = {b}")[0]["s"])
    check(area, "ClickHouse: ub.v_custom has the column as a real number column, same total", abs(ch_sum - py_sum) < 0.01, (ch_sum, py_sum))
    types = {r["name"]: r["type"] for r in R.q("SELECT name, type FROM system.columns WHERE database = 'ub' AND table = 'v_custom'")}
    check(area, "ClickHouse: typed columns (number, text, date)", types.get("TEST Discount") == "Nullable(Float64)"
          and types.get("TEST Band") == "String" and types.get("TEST Empty") == "Nullable(Date)", types)
    scope = {"utilities": [1, 2, 3], "region": None, "see_accounts": True}
    d = dashboards.page_data("custom", scope, {"period": PERIOD})
    tile = next((x for x in d["tiles"] if x["label"] == "Total TEST Discount"), None)
    check(area, "dashboard: the Total tile equals the column's sum", tile and abs(tile["value"] - py_sum) < 0.01, tile)
    bands = next((c for c in d["charts"] if c["title"] == "Revenue by TEST Band"), None)
    want_b = {}
    for r in rows:
        k = (r["extra"] or {}).get(tkey, "") or "(not set)"
        want_b[k] = want_b.get(k, 0) + num(r["AMOUNT"])
    got_b = {x["label"]: x["values"][0] for x in (bands or {}).get("rows", [])}
    check(area, "dashboard: revenue by the text column's values", bands and all(abs(got_b.get(k, 0) - v) < 0.01 for k, v in want_b.items()),
          (got_b, want_b))
    d2 = dashboards.page_data("custom", {**scope, "region": 3}, {"period": PERIOD})
    tile2 = next((x for x in d2["tiles"] if x["label"] == "Total TEST Discount"), None)
    check(area, "dashboard: the role's island limit applies (La Digue has no discount)", tile2 and (tile2["value"] or 0) == 0, tile2)
    hist = [h for h in R.history(BATCH, 20) if h["action"] == "formula" and "added the" in h["new_value"]]
    check(area, "history: one line per added column", len(hist) == 3, [h["new_value"] for h in hist])
    # uploads fill a column with the same name
    cols = ub_load.COLS + ["TEST Band"]
    sample = R.q(f"SELECT {', '.join(ub_load.COLS)} FROM ub.raw_rows WHERE batch_id = {b} LIMIT 3")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(cols)
    for i, r in enumerate(sample):
        w.writerow([r[c] if c != "BatchId" else "TEST-FORMULA-UPLOAD" for c in ub_load.COLS] + [f"from file {i}"])
    path = Path(tempfile.gettempdir()) / "formula-test-upload.tsv"
    path.write_text(buf.getvalue(), encoding="utf-8")
    try:
        ub_load.load([str(path)])
        got = R.q(f"SELECT `TEST Band` AS v FROM ub.v_custom WHERE batch_id = 'TEST-FORMULA-UPLOAD' ORDER BY line_no")
        check(area, "an upload with a column of the same name fills it", [g["v"] for g in got] == [f"from file {i}" for i in range(3)], got)
    finally:
        path.unlink()
        for t_ in ("raw_rows", "fact_lines"):
            ch(f"ALTER TABLE ub.{t_} DROP PARTITION 'TEST-FORMULA-UPLOAD'")
        for t_ in ("raw_batches", "batch_events", "raw_pending", "edit_log", "build_log", "load_log"):
            ch(f"DELETE FROM ub.{t_} WHERE batch_id = 'TEST-FORMULA-UPLOAD'")
    # remove
    bulk.remove_column(f"x:{empty['column']['key']}", USER)
    check(area, "a removed column leaves the pickers, grid and ClickHouse view",
          not any(c["name"] == "TEST Empty" for c in ub_custom.columns())
          and "TEST Empty" not in {r["name"] for r in R.q("SELECT name FROM system.columns WHERE database = 'ub' AND table = 'v_custom'")})
    F.load_custom()
    expect_error(area, "a removed column can no longer be used in a formula", lambda: F.compile_formula("[TEST Empty]"), "unknown field")
    check(area, "a new column never reuses a removed column's key",
          bulk.check({"action": "add_column", "column": {"name": "TEST Again", "kind": "text"}}) and
          ub_custom.add("TEST Again", "text", USER, bulk.RESERVED)["key"] not in (key, tkey, empty["column"]["key"]))
    discard()


def rows_by_line(n):
    return next(r for r in rows_now() if int(r["line_no"]) == int(n))


def speed():
    area = "8 Speed (a real month)"
    b = R.q("SELECT batch_id, rows FROM ub.raw_batches WHERE batch_id != 'TEST-FORMULA-BUILDER' ORDER BY rows DESC LIMIT 1")[0]
    everything = {"combine": "and", "rules": []}
    spec = {"rule": everything, "action": "update", "sets": [{"field": "AMOUNT", "formula": "ROUND([Amount] * 1.05, 2)"}]}
    p = timed(f"preview, every row of a real month ({int(b['rows']):,} rows)", lambda: bulk.preview(b["batch_id"], spec))
    check(area, f"preview of {p['matched']:,} rows in under 3 s", timings[-1][1] < 3, f"{timings[-1][1]:.2f}s")
    rule = {"combine": "and", "rules": [{"field": "REGION", "op": "eq", "value": "2"},
                                        {"combine": "or", "rules": [{"field": "TARIFFGROUPDESC", "op": "contains", "value": "domestic"},
                                                                    {"formula": "[Amount] > [Quantity] * 5"}]}]}
    timed("preview, a rule with a group and a formula condition", lambda: bulk.preview(b["batch_id"], {**spec, "rule": rule}))
    check(area, "complex rule preview in under 3 s", timings[-1][1] < 3, f"{timings[-1][1]:.2f}s")
    # the real month is only previewed here, never changed


# ----------------------------------------------------------------------------

def main():
    print("building the test month ...", flush=True)
    drop_batch()
    timed("load the test month (5,005 rows)", make_batch)
    try:
        rows = rows_now()
        print(f"test month: {len(rows):,} rows", flush=True)
        for name, fn in (("formulas", lambda: formulas(rows)), ("mistakes", mistakes), ("hostile", hostile),
                         ("conditions", lambda: conditions(rows)), ("actions", actions), ("end to end", end_to_end),
                         ("api", api), ("added columns", added_columns), ("speed", speed)):
            print(f"-- {name}", flush=True)
            try:
                fn()
            except Exception as e:  # a crash is a failure, not the end of the run
                import traceback
                check(name, "ran without crashing", False, traceback.format_exc()[-600:])
    finally:
        drop_batch()
        left = ch(f"SELECT count() FROM ub.raw_rows WHERE batch_id = {F.sql_text(BATCH)} FORMAT TSV").strip()
        check("99 Clean-up", "the test month is gone again", left == "0", left)
    areas = {}
    for a, _, ok, _ in results:
        areas.setdefault(a, [0, 0])[0 if ok else 1] += 1
    print("\n| Area | Passed | Failed |\n|---|---|---|")
    for a in sorted(areas):
        print(f"| {a} | {areas[a][0]} | {areas[a][1]} |")
    failed = [r for r in results if not r[2]]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    for a, n, _, d in failed:
        print(f"FAILED [{a}] {n}: {d}")
    print("\ntimings:")
    for label, s in timings:
        print(f"  {label}: {s:.2f}s")
    out = Path(os.environ.get("FORMULA_REPORT", tempfile.gettempdir() + "/formula-test-results.json"))
    out.write_text(json.dumps({"results": results, "timings": timings}, indent=1))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
