#!/usr/bin/env python3
"""Utility billing "Statistic Report" export -> ClickHouse database `ub`.

    python3 ub_load.py "Statistic Report24092026.csv"     # or the .zip it came in
    python3 ub_load.py a.csv b.zip                        # several files at once
    python3 ub_load.py --rebuild                          # rebuild every batch's tables now
    python3 ub_load.py --apply <batch_id> <user>          # finalize a reviewer's pending edits

Stop-gap until the SQL Server table behind the report is known (run
find_source.py to look for it). Reads the export -- tab- or comma-separated,
optionally zipped -- stores its rows as they are in ub.raw_rows, then builds
the tables the dashboards and Power BI read (ub.fact_lines and the aggregates
in ub_aggregates.sql). A new upload is live at once as a Draft; a reviewer can
edit its rows in the web app, and --apply writes those edits into the raw rows
and rebuilds (see "review workflow" in ub_schema.sql). ClickHouse settings
(CH_URL, CH_USER, CH_PASSWORD) come from .env, the same as ax_load.py.

Re-loading a file replaces its batches (BatchId column; the file name when
there is none) instead of adding them twice.

Dates: an export that went through Excel has CURDATE / INVOICEDATE reduced to
"00:00.0" and CURDATETICKS rounded to 6 digits (~1 day). Then each row's date
comes from the ticks and its billing month is the dominant month of its batch
(ub.raw_batches). A file exported straight from SQL Server, with real dates,
is used as is. Excel-style d/m/y dates are ambiguous -- prefer yyyy-mm-dd.
"""
import csv
import io
import sys
import time
import zipfile
from pathlib import Path

import ax_load  # ch(), insert(), run_sql_file(), .env handling
import ub_custom
from ax_load import ch

HERE = Path(__file__).resolve().parent
COLS = """ADDITIONALITEMSTYPE AMOUNT CONNECTIONID CURDATE CUSTID INVOICEDATE INVOICEID
INVOICEORIGIN INVOICEVALUETYPE QUANTITY REGION STATCATEGORIES TARIFFGROUPCODE TARIFFGROUPDESC
UTILITYTYPE MCSEXTERNALASSETID CURDATETICKS ADDITIONALITEMSDESCRIPTION CATEGORYDESCRIPTION
CONNECTIONMEMBERTYPE FREETEXTINVOICE INVOICEORIGINDESCRIPTION INVOICEVALUEDESCRIPTION
UTILITYTYPEDESCRIPTION SECTORDESCRIPTION SECTORGROUP REGIONNAME METERWATERNODE
METERWATERSOURCE PVINDICATION BatchId LoadDateTime""".split()
OPTIONAL = {"BatchId", "LoadDateTime", "CURDATETICKS", "METERWATERNODE", "METERWATERSOURCE",
            "PVINDICATION", "CONNECTIONMEMBERTYPE", "FREETEXTINVOICE"}
CHUNK = 50_000

RAW_COLS = ", ".join(["line_no"] + COLS)

# The month most of a batch's statistics dates fall in: the billing period of
# its rows that name no date of their own. Same date rules as ub.v_lines.
BATCH_MONTHS = """
INSERT INTO ub.raw_batches (batch_id, batch_month, file_name, rows)
SELECT batch_id, topKIf(1)(toStartOfMonth(stat_date), stat_date > '1970-01-01')[1], any(file_name), count()
FROM ub.v_lines WHERE batch_id IN ({batches}) GROUP BY batch_id
"""


def decode(data: bytes) -> str:
    """UTF-8 if it is; else Windows-1252, which is what Excel writes."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def xlsx_text(data: bytes) -> str:
    """An .xlsx sheet as tab-separated text. Real dates survive here as
    yyyy-mm-dd -- unlike a CSV that Excel has re-saved."""
    from auto_load import read_xlsx  # needs openpyxl
    header, rows = read_xlsx(data)
    clean = lambda v: str(v).replace("\t", " ").replace("\n", " ")
    return "\n".join("\t".join(clean(v) for v in r) for r in [header, *rows])


def read_rows(path: Path):
    """Yield (source name, text) for a csv/tsv/txt/xlsx file or each one inside a zip."""
    def one(name, data):
        return xlsx_text(data) if name.lower().endswith((".xlsx", ".xlsm")) else decode(data)

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith((".csv", ".tsv", ".txt", ".xlsx", ".xlsm")):
                    yield name, one(name, z.read(name))
    else:
        yield path.name, one(path.name, path.read_bytes())


def stage(name: str, text: str) -> int:
    first = text.split("\n", 1)[0]
    reader = csv.reader(io.StringIO(text), delimiter="\t" if "\t" in first else ",")
    header = [h.strip() for h in next(reader)]
    pos = {h.upper(): i for i, h in enumerate(header) if h}
    missing = [c for c in COLS if c.upper() not in pos and c not in OPTIONAL]
    if missing:
        sys.exit(f"{name}: columns missing from the export: {', '.join(missing)}")
    idx = [pos.get(c.upper()) for c in COLS]
    fallback_batch = Path(name).stem
    # columns a reviewer added, if the file has them (matched by name)
    added = [(c["key"], pos[c["name"].upper()]) for c in ub_custom.columns() if c["name"].upper() in pos]
    if added:
        print(f"{name}: also reading added column(s): "
              + ", ".join(c["name"] for c in ub_custom.columns() if c["name"].upper() in pos), flush=True)

    ch("TRUNCATE TABLE ub.raw_load")
    batch, n = [], 0
    for rec in reader:
        if not any(f.strip() for f in rec):
            continue
        n += 1
        vals = []
        for c, i in zip(COLS, idx):
            v = rec[i].replace("\xa0", " ").strip() if i is not None and i < len(rec) else ""
            vals.append("" if v == "NULL" else v)
        if not vals[COLS.index("BatchId")]:
            vals[COLS.index("BatchId")] = fallback_batch
        extra = {k: rec[i].strip() for k, i in added if i < len(rec) and rec[i].strip() not in ("", "NULL")}
        batch.append([n] + vals + [extra])
        if len(batch) == CHUNK:
            ax_load.insert("ub.raw_load", ["line_no"] + COLS + ["extra"], batch)
            batch = []
    if batch:
        ax_load.insert("ub.raw_load", ["line_no"] + COLS + ["extra"], batch)
    return n


def sql_list(items) -> str:
    return ", ".join("'" + str(i).replace("\\", "\\\\").replace("'", "\\'") + "'" for i in items)


def ensure_schema() -> None:
    """Tables and views, the one-time move of an older install onto raw rows, and a build of
    any batch that has raw rows but no dashboard tables yet."""
    ax_load.run_sql_file(HERE / "ub_schema.sql")
    engine = ch("SELECT engine FROM system.tables WHERE database = 'ub' AND name = 'fact_billing' FORMAT TSV").strip()
    if engine and engine != "View":
        migrate()
    ax_load.run_sql_file(HERE / "ub_views.sql")
    # fact_lines holds v_lines' columns: if v_lines changed (a new version of the app), rebuild it whole
    cols = lambda t: ch(f"SELECT name, type FROM system.columns WHERE database = 'ub' AND table = '{t}' "
                        "ORDER BY position FORMAT TSV")
    if cols("fact_lines") != cols("v_lines"):
        print("the billing lines gained columns: rebuilding them ...", flush=True)
        ch("DROP TABLE ub.fact_lines")
        ax_load.run_sql_file(HERE / "ub_views.sql")  # recreates fact_lines empty-structured and fills it
        ch("TRUNCATE TABLE ub.fact_lines")
    ub_custom.refresh_view()
    # batches with no status yet (an older install) start as uploaded: Draft, revision 1
    ch("INSERT INTO ub.batch_events (batch_id, status, revision, changed_by, note) "
       "SELECT DISTINCT batch_id, 'draft', 1, 'system', 'uploaded' FROM ub.raw_batches "
       "WHERE batch_id NOT IN (SELECT batch_id FROM ub.batch_events)")
    missing = ch("SELECT DISTINCT batch_id FROM ub.raw_batches WHERE batch_id NOT IN "
                 "(SELECT DISTINCT batch_id FROM ub.fact_lines) FORMAT TSV").split()
    if missing:
        build(missing, "first build")


# The fact table of an older install, written back as the raw rows it was parsed
# from: the same text for every field the parse reads, so ub.v_lines gives back
# the same lines, months and amounts.
MIGRATE = """
INSERT INTO ub.raw_rows (batch_id, file_name, line_no, ADDITIONALITEMSTYPE, AMOUNT, CONNECTIONID, CURDATE,
    CUSTID, INVOICEDATE, INVOICEID, INVOICEORIGIN, INVOICEVALUETYPE, QUANTITY, REGION, STATCATEGORIES,
    TARIFFGROUPCODE, TARIFFGROUPDESC, UTILITYTYPE, MCSEXTERNALASSETID, CURDATETICKS, ADDITIONALITEMSDESCRIPTION,
    CATEGORYDESCRIPTION, CONNECTIONMEMBERTYPE, FREETEXTINVOICE, INVOICEORIGINDESCRIPTION, INVOICEVALUEDESCRIPTION,
    UTILITYTYPEDESCRIPTION, SECTORDESCRIPTION, SECTORGROUP, REGIONNAME, METERWATERNODE, METERWATERSOURCE,
    PVINDICATION, BatchId, LoadDateTime)
SELECT f.batch_id, ifNull(l.file_name, f.batch_id), f.line_no, toString(f.charge_type), toString(f.amount), f.connection_id,
    if(f.date_exact AND f.stat_date > '1970-01-01', toString(f.stat_date), ''),
    f.customer_id, if(f.date_exact AND f.invoice_date > '1970-01-01', toString(f.invoice_date), ''), f.invoice_id,
    toString(f.invoice_origin), toString(f.value_type), toString(f.quantity), toString(f.region_code), f.category_id,
    f.tariff_code, f.tariff_desc, toString(f.utility_code), f.meter_id,
    if(f.stat_date > '1970-01-01', toString((toUnixTimestamp(toDateTime(f.stat_date)) + 62135596800) * 10000000), ''),
    f.charge_desc, f.category_desc, toString(f.member_type), toString(f.is_free_text), f.origin_desc, f.value_desc,
    f.utility_name, f.sector_desc, f.sector_code, f.region_name, f.water_node, f.water_source,
    toString(f.pv_connection), f.batch_id, ''
FROM ub.fact_billing_old AS f
LEFT JOIN (SELECT batch_id, any(file_name) AS file_name FROM ub.load_log GROUP BY batch_id) AS l ON l.batch_id = f.batch_id
SETTINGS join_use_nulls = 1
"""


def migrate() -> None:
    print("moving the loaded billing lines to the raw store (one time) ...", flush=True)
    ch("RENAME TABLE ub.fact_billing TO ub.fact_billing_old")  # the name is the view's from now on
    ax_load.run_sql_file(HERE / "ub_views.sql")
    if int(ch("SELECT count() FROM ub.raw_rows FORMAT TSV")) == 0:
        ch(MIGRATE)
        batches = ch("SELECT DISTINCT batch_id FROM ub.raw_rows FORMAT TSV").split()
        ch("TRUNCATE TABLE ub.raw_batches")
        ch(BATCH_MONTHS.format(batches=sql_list(batches)))
    old = ch("SELECT batch_id, period_month, count(), sum(amount) FROM ub.fact_billing_old "
             "GROUP BY 1, 2 ORDER BY 1, 2 FORMAT TSV")
    new = ch("SELECT batch_id, period, count(), sum(amount) FROM ub.v_lines GROUP BY 1, 2 ORDER BY 1, 2 FORMAT TSV")
    if old != new:
        sys.exit("migration check failed -- the old lines are kept in ub.fact_billing_old\n"
                 f"before:\n{old}\nafter:\n{new}")
    ch("DROP TABLE ub.fact_billing_old")
    ch("TRUNCATE TABLE ub.fact_lines")  # built next, by ensure_schema
    for t in ("app_billing", "app_customer_period", "app_connection_period"):
        ch(f"DROP TABLE IF EXISTS ub.{t}")
    print(f"moved: every batch, month, line count and amount matches\n{new}", flush=True)


# ============ build: the tables the dashboards and Power BI read ============

def build(batches: list[str], reason: str) -> None:
    """Rebuild these batches' dashboard lines (fact_lines) from their raw rows, then the Power
    BI aggregates. The web app's cached results end with the build_log entry."""
    for b in batches:
        t0 = time.time()
        print(f"building the dashboard tables for batch {b} ...", flush=True)
        ch(f"ALTER TABLE ub.fact_lines DROP PARTITION {sql_list([b])}")
        ch(f"INSERT INTO ub.fact_lines SELECT * FROM ub.v_lines WHERE batch_id = {sql_list([b])}")
        n = int(ch(f"SELECT count() FROM ub.fact_lines WHERE batch_id = {sql_list([b])} FORMAT TSV"))
        ch("INSERT INTO ub.build_log (batch_id, rows, seconds, reason) VALUES "
           f"({sql_list([b])}, {n}, {time.time() - t0:.2f}, {sql_list([reason])})")
    t0 = time.time()
    print("building the aggregates ...", flush=True)
    ax_load.run_sql_file(HERE / "ub_aggregates.sql")
    print(f"aggregates and dashboards built  {time.time() - t0:.1f}s", flush=True)


def status_of(batch: str) -> tuple[str, int]:
    r = ch(f"SELECT status, revision FROM ub.v_batch_status WHERE batch_id = {sql_list([batch])} FORMAT TSV").split()
    return (r[0], int(r[1])) if r else ("draft", 0)


def set_status(batch: str, status: str, revision: int, user: str, note: str) -> None:
    ch("INSERT INTO ub.batch_events (batch_id, status, revision, changed_by, note) VALUES "
       f"({sql_list([batch])}, {sql_list([status])}, {int(revision)}, {sql_list([user])}, {sql_list([note])})")


def apply(batch: str, user: str) -> None:
    """Finalize a reviewer's edits: write them into the batch's raw rows, rebuild its aggregates and
    dashboards, and make it a Draft of the next revision, waiting to be marked Reviewed."""
    ensure_schema()
    b = sql_list([batch])
    pending = int(ch(f"SELECT count() FROM ub.raw_pending FINAL WHERE batch_id = {b} FORMAT TSV"))
    if not pending:
        sys.exit("nothing to finalize: this batch has no pending edits")
    status, revision = status_of(batch)
    print(f"finalizing {pending} edited row(s) of batch {batch} (revision {revision} -> {revision + 1}) ...", flush=True)
    t0 = time.time()
    # the batch's rows with the edits in, built beside raw_rows, then swapped in whole
    ch("DROP TABLE IF EXISTS ub.raw_rows_next")
    ch("CREATE TABLE ub.raw_rows_next AS ub.raw_rows")  # same structure, or REPLACE PARTITION refuses
    fname = f"(SELECT any(file_name) FROM ub.raw_batches WHERE batch_id = {b})"
    ch(f"INSERT INTO ub.raw_rows_next (batch_id, file_name, {RAW_COLS}, extra) "
       f"SELECT batch_id, file_name, {RAW_COLS}, extra FROM ub.raw_rows WHERE batch_id = {b} "
       f"AND line_no NOT IN (SELECT line_no FROM ub.raw_pending FINAL WHERE batch_id = {b}) "
       f"UNION ALL "
       f"SELECT batch_id, {fname}, {RAW_COLS}, extra FROM ub.raw_pending FINAL WHERE batch_id = {b} AND action != 'delete'")
    rows = int(ch(f"SELECT count() FROM ub.raw_rows_next FORMAT TSV"))
    if rows == 0:  # a month with no rows would drop out of review with no way back but a re-upload
        ch("TRUNCATE TABLE ub.raw_rows_next")
        sys.exit("refused: the edits would delete every row of this batch. Upload its file again to replace it.")
    ch(f"ALTER TABLE ub.raw_rows REPLACE PARTITION {b} FROM ub.raw_rows_next")
    ch("TRUNCATE TABLE ub.raw_rows_next")
    ch(f"DELETE FROM ub.raw_pending WHERE batch_id = {b}")
    # an edited date can move the batch's month
    ch(f"DELETE FROM ub.raw_batches WHERE batch_id = {b}")
    ch(BATCH_MONTHS.format(batches=b))
    print(f"{rows:,} rows written with the edits in  {time.time() - t0:.1f}s", flush=True)
    ch("INSERT INTO ub.edit_log (batch_id, action, new_value, user, revision) VALUES "
       f"({b}, 'finalize', '{pending} row(s)', {sql_list([user])}, {revision + 1})")
    build([batch], f"finalized edits, revision {revision + 1}")
    set_status(batch, "draft", revision + 1, user, f"{pending} edited row(s) finalized")
    print(f"revision {revision + 1} built -- a Draft until it is marked Reviewed", flush=True)


def load(paths: list[str]) -> None:
    ensure_schema()
    loaded = []
    for p in paths:
        print(f"reading {Path(p).name} ...", flush=True)
        for name, text in read_rows(Path(p)):
            t0 = time.time()
            print(f"{name}: staging raw rows into ClickHouse ...", flush=True)
            n = stage(name, text)
            batches = ch("SELECT DISTINCT BatchId FROM ub.raw_load FORMAT TSV").split()
            print(f"{name}: {n:,} rows staged, storing them as raw rows ...", flush=True)
            for b in batches:  # a re-load replaces, never doubles
                ch(f"ALTER TABLE ub.raw_rows DROP PARTITION {sql_list([b])}")
                ch(f"DELETE FROM ub.raw_batches WHERE batch_id = {sql_list([b])}")
            fname = sql_list([name])
            ch(f"INSERT INTO ub.raw_rows (batch_id, file_name, {RAW_COLS}, extra) "
               f"SELECT BatchId, {fname}, {RAW_COLS}, extra FROM ub.raw_load")
            ch(BATCH_MONTHS.format(batches=sql_list(batches)))
            ch("INSERT INTO ub.load_log (file_name, batch_id, rows, amount) "
               f"SELECT {fname}, batch_id, count(), sum(amount) FROM ub.v_lines "
               f"WHERE batch_id IN ({sql_list(batches)}) GROUP BY batch_id")
            src = ch("SELECT count(), round(sum(toFloat64OrZero(AMOUNT)), 2) FROM ub.raw_load FORMAT TSV").split()
            dst = ch(f"SELECT count(), round(sum(toFloat64(amount)), 2) FROM ub.v_lines "
                     f"WHERE batch_id IN ({sql_list(batches)}) FORMAT TSV").split()
            ch("TRUNCATE TABLE ub.raw_load")
            print(f"{name}: {n:,} rows read, {int(dst[0]):,} stored, amount {float(dst[1]):,.2f} "
                  f"(file says {float(src[1]):,.2f}), {len(batches)} batch(es), {time.time() - t0:.0f}s")
            if src[0] != dst[0]:
                print(f"  WARNING: {src[0]} staged vs {dst[0]} stored")
            for b in batches:  # a fresh upload is live at once, as a Draft waiting for review
                ch(f"DELETE FROM ub.raw_pending WHERE batch_id = {sql_list([b])}")
                set_status(b, "draft", status_of(b)[1] + 1, "upload", f"uploaded from {name}")
            loaded.extend(batches)
    build(loaded, "upload")
    print("uploaded: aggregates and dashboards built, status Draft until reviewed", flush=True)
    print(ch("SELECT * FROM ub.v_batches ORDER BY period_month FORMAT PrettyCompactNoEscapes"))


def rebuild_all() -> None:
    """Every batch's dashboard tables and the aggregates, from the raw rows as they are now."""
    ensure_schema()
    build(ch("SELECT DISTINCT batch_id FROM ub.raw_batches FORMAT TSV").split(), "rebuild")
    print(ch("SELECT * FROM ub.v_batches ORDER BY period_month FORMAT PrettyCompactNoEscapes"))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args in (["--rebuild"], ["--aggregates"], ["--powerbi"]):  # older names still work
        rebuild_all()
    elif len(args) == 3 and args[0] == "--apply":
        apply(args[1], args[2])
    elif args == ["--schema"]:
        ensure_schema()
    else:
        load(args)
