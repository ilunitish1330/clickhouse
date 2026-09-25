#!/usr/bin/env python3
"""Utility billing "Statistic Report" export -> ClickHouse database `ub`.

    python3 ub_load.py "Statistic Report24092026.csv"     # or the .zip it came in
    python3 ub_load.py a.csv b.zip                        # several files at once
    python3 ub_load.py --powerbi                          # build the Power BI tables now

Stop-gap until the SQL Server table behind the report is known (run
find_source.py to look for it). Reads the export -- tab- or comma-separated,
optionally zipped -- and stores its rows as they are in ub.raw_rows. That is
the whole load: nothing is aggregated here. The web app parses and aggregates
the raw rows a dashboard needs when it is opened (ub_views.sql), and the Power
BI tables (ub_aggregates.sql) are built from them by --powerbi, when Power BI
is about to refresh. ClickHouse settings (CH_URL, CH_USER, CH_PASSWORD) come
from .env, the same as ax_load.py.

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
        batch.append([n] + vals)
        if len(batch) == CHUNK:
            ax_load.insert("ub.raw_load", ["line_no"] + COLS, batch)
            batch = []
    if batch:
        ax_load.insert("ub.raw_load", ["line_no"] + COLS, batch)
    return n


def sql_list(items) -> str:
    return ", ".join("'" + str(i).replace("\\", "\\\\").replace("'", "\\'") + "'" for i in items)


def ensure_schema() -> None:
    """Tables and views, and the one-time move of an older install onto raw rows."""
    ax_load.run_sql_file(HERE / "ub_schema.sql")
    engine = ch("SELECT engine FROM system.tables WHERE database = 'ub' AND name = 'fact_billing' FORMAT TSV").strip()
    if engine and engine != "View":
        migrate()
    ax_load.run_sql_file(HERE / "ub_views.sql")


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
    for t in ("app_billing", "app_customer_period", "app_connection_period"):
        ch(f"DROP TABLE IF EXISTS ub.{t}")
    print(f"moved: every batch, month, line count and amount matches\n{new}", flush=True)


def load(paths: list[str]) -> None:
    ensure_schema()
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
            ch(f"INSERT INTO ub.raw_rows (batch_id, file_name, {RAW_COLS}) "
               f"SELECT BatchId, {fname}, {RAW_COLS} FROM ub.raw_load")
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
    print("raw rows stored -- dashboards aggregate them when they are opened", flush=True)
    print(ch("SELECT * FROM ub.v_batches ORDER BY period_month FORMAT PrettyCompactNoEscapes"))


def powerbi() -> None:
    """The tables the Power BI report imports, built from the raw rows now."""
    ensure_schema()
    t0 = time.time()
    print("building the Power BI tables from the raw rows ...", flush=True)
    ax_load.run_sql_file(HERE / "ub_aggregates.sql")
    print(f"Power BI tables built  {time.time() - t0:.1f}s")
    print(ch("SELECT * FROM ub.v_batches ORDER BY period_month FORMAT PrettyCompactNoEscapes"))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args in (["--powerbi"], ["--aggregates"]):  # --aggregates: the old name
        powerbi()
    elif args == ["--schema"]:
        ensure_schema()
    else:
        load(args)
