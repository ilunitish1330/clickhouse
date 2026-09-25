#!/usr/bin/env python3
"""Utility billing "Statistic Report" export -> ClickHouse database `ub`.

    python3 ub_load.py "Statistic Report24092026.csv"     # or the .zip it came in
    python3 ub_load.py a.csv b.zip                        # several files at once
    python3 ub_load.py --aggregates                       # rebuild dims + aggregates only

Stop-gap until the SQL Server table behind the report is known (run
find_source.py to look for it). Reads the export -- tab- or comma-separated,
optionally zipped -- stages it as text, then parses it inside ClickHouse into
ub.fact_billing and rebuilds ub_aggregates.sql. ClickHouse settings (CH_URL,
CH_USER, CH_PASSWORD) come from .env, the same as ax_load.py.

Re-loading a file replaces its batches (BatchId column; the file name when
there is none) instead of adding them twice.

Dates: an export that went through Excel has CURDATE / INVOICEDATE reduced to
"00:00.0" and CURDATETICKS rounded to 6 digits (~1 day). Then each row's date
comes from the ticks and its billing month is the dominant month of its batch.
A file exported straight from SQL Server, with real dates, is used as is.
Excel-style d/m/y dates are ambiguous -- prefer yyyy-mm-dd exports.
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

IS_DATE = r"'^\\d{4}-\\d{2}-\\d{2}|^\\d{1,2}/\\d{1,2}/\\d{4}'"

TRANSFORM = f"""
INSERT INTO ub.fact_billing
    (batch_id, line_no, period_month, stat_date, invoice_date, date_exact, invoice_id,
     invoice_prefix, customer_id, connection_id, meter_id, utility_code, utility_name,
     region_code, region_name, tariff_code, tariff_desc, category_id, category_desc,
     sector_code, sector_desc, charge_type, charge_desc, is_pv, value_type, value_desc,
     invoice_origin, origin_desc, is_free_text, member_type, pv_connection, water_node,
     water_source, amount, quantity)
WITH r AS (
    SELECT *,
           match(CURDATE, {IS_DATE})     AS cur_ok,
           match(INVOICEDATE, {IS_DATE}) AS inv_ok,
           if(inv_ok, toDate(parseDateTimeBestEffort(INVOICEDATE)), toDate('1970-01-01')) AS inv_d,
           multiIf(cur_ok, toDate(parseDateTimeBestEffort(CURDATE)),
                   toFloat64OrZero(CURDATETICKS) > 0,
                   toDate(toDateTime(toInt64(toFloat64OrZero(CURDATETICKS) / 1e7 - 62135596800))),
                   toDate('1970-01-01')) AS stat_d
    FROM ub.raw_load
),
b AS (
    SELECT BatchId, topK(1)(toStartOfMonth(stat_d))[1] AS batch_month
    FROM r WHERE stat_d > '1970-01-01' GROUP BY BatchId
)
SELECT r.BatchId, r.line_no,
       multiIf(r.inv_ok, toStartOfMonth(r.inv_d), r.cur_ok, toStartOfMonth(r.stat_d), b.batch_month),
       r.stat_d, r.inv_d, toUInt8(r.inv_ok OR r.cur_ok),
       r.INVOICEID, extract(r.INVOICEID, '^[A-Za-z]+-?'), r.CUSTID, r.CONNECTIONID,
       r.MCSEXTERNALASSETID,
       toUInt8OrZero(r.UTILITYTYPE), r.UTILITYTYPEDESCRIPTION,
       toUInt8OrZero(r.REGION), r.REGIONNAME,
       r.TARIFFGROUPCODE, r.TARIFFGROUPDESC, r.STATCATEGORIES, r.CATEGORYDESCRIPTION,
       r.SECTORGROUP, r.SECTORDESCRIPTION,
       toUInt8OrZero(r.ADDITIONALITEMSTYPE), r.ADDITIONALITEMSDESCRIPTION,
       toUInt8(r.ADDITIONALITEMSDESCRIPTION = 'PV'),
       toUInt8OrZero(r.INVOICEVALUETYPE), r.INVOICEVALUEDESCRIPTION,
       toUInt8OrZero(r.INVOICEORIGIN), r.INVOICEORIGINDESCRIPTION,
       toUInt8OrZero(r.FREETEXTINVOICE), toUInt8OrZero(r.CONNECTIONMEMBERTYPE),
       toUInt8OrZero(r.PVINDICATION), r.METERWATERNODE, r.METERWATERSOURCE,
       toDecimal64OrZero(toString(round(toFloat64OrZero(r.AMOUNT), 4)), 4),
       toDecimal64OrZero(toString(round(toFloat64OrZero(r.QUANTITY), 4)), 4)
FROM r LEFT JOIN b ON b.BatchId = r.BatchId
"""
# ponytail: amounts go float -> text -> Decimal, not float -> Decimal. The
# direct cast truncates: 1070.37 is 1070.36999.. as a float and lands as
# 1070.3699 (8,642 values in the first file, 0.85 short in total). Via text the
# shortest float repr '1070.37' parses exactly. Float first so Excel's
# scientific notation (2E-14, 1.5E+06) still parses.


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


def load(paths: list[str]) -> None:
    ax_load.run_sql_file(HERE / "ub_schema.sql")
    for p in paths:
        print(f"reading {Path(p).name} ...", flush=True)
        for name, text in read_rows(Path(p)):
            t0 = time.time()
            print(f"{name}: staging raw rows into ClickHouse ...", flush=True)
            n = stage(name, text)
            print(f"{name}: {n:,} rows staged, building the fact table ...", flush=True)
            batches = ch("SELECT DISTINCT BatchId FROM ub.raw_load FORMAT TSV").split()
            for b in batches:  # a re-load replaces, never doubles
                ch(f"ALTER TABLE ub.fact_billing DROP PARTITION '{b}'")
            ch(TRANSFORM)
            ch("INSERT INTO ub.load_log (file_name, batch_id, rows, amount) "
               "SELECT {f:String}, batch_id, count(), sum(amount) FROM ub.fact_billing "
               "WHERE batch_id IN (SELECT DISTINCT BatchId FROM ub.raw_load) GROUP BY batch_id"
               .replace("{f:String}", "'" + name.replace("'", "") + "'"))
            src = ch("SELECT count(), round(sum(toFloat64OrZero(AMOUNT)), 2) FROM ub.raw_load FORMAT TSV").split()
            dst = ch("SELECT count(), round(sum(toFloat64(amount)), 2) FROM ub.fact_billing "
                     "WHERE batch_id IN (SELECT DISTINCT BatchId FROM ub.raw_load) FORMAT TSV").split()
            ch("TRUNCATE TABLE ub.raw_load")
            print(f"{name}: {n:,} rows read, {int(dst[0]):,} loaded, amount {float(dst[1]):,.2f} "
                  f"(file says {float(src[1]):,.2f}), {len(batches)} batch(es), {time.time() - t0:.0f}s")
            if src[0] != dst[0]:
                print(f"  WARNING: {src[0]} staged vs {dst[0]} loaded")
    aggregates()


def aggregates() -> None:
    t0 = time.time()
    print("rebuilding dimensions and aggregates ...", flush=True)
    ax_load.run_sql_file(HERE / "ub_aggregates.sql")
    print(f"ub aggregates rebuilt  {time.time() - t0:.1f}s")
    print(ch("SELECT * FROM ub.v_batches ORDER BY period_month FORMAT PrettyCompactNoEscapes"))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    aggregates() if args == ["--aggregates"] else load(args)
