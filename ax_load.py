#!/usr/bin/env python3
"""Dynamics AX 2012 (SQL Server) -> ClickHouse star schema + aggregates, for Power BI.

    pip install pymssql httpx
    cp .env.example .env            # fill in the SQL Server + ClickHouse details
    python3 ax_load.py --check      # can we reach SQL Server? row count per source table
    python3 ax_load.py --init       # create the ax database and tables (ch_schema.sql)
    python3 ax_load.py              # full load of every table, then rebuild aggregates
    python3 ax_load.py fact_sales dim_item      # reload just these, then aggregates
    python3 ax_load.py --aggregates # rebuild aggregates.sql only

Must run on a machine that can reach both SQL Server (1433) and ClickHouse
(HTTP, 8123) -- e.g. the SQL Server box itself or the ClickHouse box.

Each table is reloaded in full into ax.<table>__load and swapped in with
EXCHANGE TABLES, which is atomic: Power BI sees the old table or the new one,
never a half-loaded one, and rows deleted in AX disappear. See ch_schema.sql
for why this beats incremental loads at this data size.

ponytail: no staging layer in SQL Server, no Airflow. Each SELECT below is the
transform; schedule this script with cron / Windows Task Scheduler.

ponytail: pymssql, not pyodbc, because this server only offers TLS 1.0 and
needs TDS 7.0 (see schema_dump.py). Override with MSSQL_TDS_VERSION.
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
CHUNK = 20_000


def load_dotenv(path: Path = HERE / ".env") -> None:
    """KEY=VALUE lines into os.environ; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()
CH = os.environ.get("CH_URL", "http://localhost:8123/")
CH_AUTH = {"X-ClickHouse-User": os.environ.get("CH_USER", "default"),
           "X-ClickHouse-Key": os.environ.get("CH_PASSWORD", "")}
client = httpx.Client(timeout=600.0, headers=CH_AUTH)

# =============================================================================
# The transforms. One SELECT per ClickHouse table; the column aliases ARE the
# ClickHouse column names. `base` is the AX table that sets the grain: the
# loader compares its COUNT(*) with the rows read, which catches a join that
# fans the grain out. Dates go through CONVERT(date, ...) -- the loader turns
# AX's empty 1900-01-01 into 1970-01-01, Date's floor.
# =============================================================================

TABLES: dict[str, tuple[str, str]] = {}

# --- dimensions --------------------------------------------------------------

# DIRPARTYTABLE is the global address book; CUSTTABLE/VENDTABLE.PARTY points at
# its RECID, 1:1, so the name folds in without changing the grain.
TABLES["dim_customer"] = ("CUSTTABLE", """
SELECT t.DATAAREAID AS data_area, t.ACCOUNTNUM AS customer_id,
       ISNULL(p.NAME, '') AS name, ISNULL(p.NAMEALIAS, '') AS name_alias,
       t.CUSTGROUP AS group_code, t.CURRENCY AS currency, t.PAYMTERMID AS payment_terms,
       t.CREDITMAX AS credit_limit, t.BLOCKED AS blocked,
       t.INVOICEACCOUNT AS invoice_account, t.RECID AS recid
FROM CUSTTABLE t
LEFT JOIN DIRPARTYTABLE p ON p.RECID = t.PARTY
""")

TABLES["dim_vendor"] = ("VENDTABLE", """
SELECT t.DATAAREAID AS data_area, t.ACCOUNTNUM AS vendor_id,
       ISNULL(p.NAME, '') AS name, ISNULL(p.NAMEALIAS, '') AS name_alias,
       t.VENDGROUP AS group_code, t.CURRENCY AS currency, t.PAYMTERMID AS payment_terms,
       t.BLOCKED AS blocked, t.INVOICEACCOUNT AS invoice_account, t.RECID AS recid
FROM VENDTABLE t
LEFT JOIN DIRPARTYTABLE p ON p.RECID = t.PARTY
""")

# INVENTTABLE has no name column in AX 2012: the name is per language on the
# global product. OUTER APPLY TOP 1 picks one (en-us first) so a product with
# three translations still yields one item row.
TABLES["dim_item"] = ("INVENTTABLE", """
SELECT i.DATAAREAID AS data_area, i.ITEMID AS item_id,
       ISNULL(n.NAME, '') AS item_name, i.NAMEALIAS AS name_alias,
       ISNULL(p.DISPLAYPRODUCTNUMBER, '') AS product_number, i.ITEMTYPE AS item_type,
       i.PRIMARYVENDORID AS primary_vendor, i.ITEMBUYERGROUPID AS buyer_group,
       i.COSTGROUPID AS cost_group, i.PRODGROUPID AS prod_group, i.RECID AS recid
FROM INVENTTABLE i
LEFT JOIN ECORESPRODUCT p ON p.RECID = i.PRODUCT
OUTER APPLY (SELECT TOP 1 tr.NAME FROM ECORESPRODUCTTRANSLATION tr
             WHERE tr.PRODUCT = i.PRODUCT
             ORDER BY CASE WHEN tr.LANGUAGEID = 'en-us' THEN 0 ELSE 1 END, tr.LANGUAGEID) n
""")

TABLES["dim_project"] = ("PROJTABLE", """
SELECT t.DATAAREAID AS data_area, t.PROJID AS proj_id, t.NAME AS name,
       t.PROJGROUPID AS proj_group, t.PARENTID AS parent_id, t.CUSTACCOUNT AS cust_account,
       t.STATUS AS status, t.TYPE AS proj_type,
       CONVERT(date, t.CREATED) AS created_date, CONVERT(date, t.STARTDATE) AS start_date,
       CONVERT(date, t.ENDDATE) AS end_date, t.RECID AS recid
FROM PROJTABLE t
""")

TABLES["dim_purch_order"] = ("PURCHTABLE", """
SELECT t.DATAAREAID AS data_area, t.PURCHID AS purch_id, t.PURCHNAME AS purch_name,
       t.ORDERACCOUNT AS vendor_account, t.INVOICEACCOUNT AS invoice_account,
       t.VENDGROUP AS vend_group, t.CURRENCYCODE AS currency,
       t.PURCHSTATUS AS purch_status, t.DOCUMENTSTATUS AS document_status,
       t.PURCHASETYPE AS purchase_type, CONVERT(date, t.DELIVERYDATE) AS delivery_date,
       CONVERT(date, t.CREATEDDATETIME) AS created_date, t.PROJID AS proj_id,
       t.INVENTSITEID AS site, t.INVENTLOCATIONID AS warehouse, t.RECID AS recid
FROM PURCHTABLE t
""")

# --- facts -------------------------------------------------------------------

# SALESLINE is the grain; SALESTABLE contributes header attributes.
TABLES["fact_sales"] = ("SALESLINE", """
SELECT t.DATAAREAID AS data_area, t.SALESID AS sales_id, t.LINENUM AS line_num,
       t.CUSTACCOUNT AS cust_account, t.ITEMID AS item_id, t.CURRENCYCODE AS currency,
       CONVERT(date, ISNULL(h.CREATEDDATETIME, t.CREATEDDATETIME)) AS order_date,
       CONVERT(date, t.SHIPPINGDATEREQUESTED) AS ship_date,
       t.SALESSTATUS AS sales_status, ISNULL(h.DOCUMENTSTATUS, 0) AS document_status,
       t.QTYORDERED AS qty_ordered, t.SALESPRICE AS sales_price, t.COSTPRICE AS cost_price,
       t.LINEAMOUNT AS line_amount, t.LINEDISC AS line_disc,
       ISNULL(h.SALESNAME, '') AS sales_name, t.PROJID AS proj_id,
       t.INVENTTRANSID AS invent_trans_id, t.RECID AS recid
FROM SALESLINE t
LEFT JOIN SALESTABLE h ON h.SALESID = t.SALESID AND h.DATAAREAID = t.DATAAREAID
""")

TABLES["fact_cust_invoice"] = ("CUSTINVOICEJOUR", """
SELECT t.DATAAREAID AS data_area, t.INVOICEID AS invoice_id,
       CONVERT(date, t.INVOICEDATE) AS invoice_date, CONVERT(date, t.DUEDATE) AS due_date,
       t.SALESID AS sales_id, t.INVOICEACCOUNT AS cust_account,
       t.ORDERACCOUNT AS order_account, t.CUSTGROUP AS cust_group,
       t.CURRENCYCODE AS currency, t.QTY AS qty, t.INVOICEAMOUNT AS invoice_amount,
       t.INVOICEAMOUNTMST AS invoice_amount_mst, t.SALESBALANCEMST AS sales_balance_mst,
       t.SUMTAXMST AS tax_mst, t.SUMLINEDISCMST AS line_disc_mst,
       t.LEDGERVOUCHER AS ledger_voucher, t.RECID AS recid
FROM CUSTINVOICEJOUR t
""")

TABLES["fact_cust_trans"] = ("CUSTTRANS", """
SELECT t.DATAAREAID AS data_area, t.ACCOUNTNUM AS cust_account,
       t.CURRENCYCODE AS currency, CONVERT(date, t.TRANSDATE) AS trans_date,
       CONVERT(date, t.DUEDATE) AS due_date, CONVERT(date, t.CLOSED) AS closed,
       t.TRANSTYPE AS trans_type, t.AMOUNTCUR AS amount_cur, t.AMOUNTMST AS amount_mst,
       t.SETTLEAMOUNTCUR AS settle_cur, t.SETTLEAMOUNTMST AS settle_mst,
       t.VOUCHER AS voucher, t.INVOICE AS invoice, t.TXT AS txt, t.RECID AS recid
FROM CUSTTRANS t
""")

TABLES["fact_vend_invoice"] = ("VENDINVOICEJOUR", """
SELECT t.DATAAREAID AS data_area, t.INVOICEID AS invoice_id,
       CONVERT(date, t.INVOICEDATE) AS invoice_date, CONVERT(date, t.DUEDATE) AS due_date,
       t.PURCHID AS purch_id, t.INVOICEACCOUNT AS vendor_account,
       t.ORDERACCOUNT AS order_account, t.VENDGROUP AS vend_group,
       t.CURRENCYCODE AS currency, t.QTY AS qty, t.INVOICEAMOUNT AS invoice_amount,
       t.INVOICEAMOUNTMST AS invoice_amount_mst, t.SUMTAX AS tax,
       t.SUMLINEDISC AS line_disc, t.LEDGERVOUCHER AS ledger_voucher,
       t.INTERNALINVOICEID AS internal_invoice_id, t.RECID AS recid
FROM VENDINVOICEJOUR t
""")

# The line carries no vendor; it comes from the journal header. The header's
# key is (PURCHID, INVOICEID, INVOICEDATE, NUMBERSEQUENCEGROUP, INTERNALINVOICEID)
# -- OUTER APPLY TOP 1 on it, so even a duplicate header cannot double a line.
TABLES["fact_vend_invoice_line"] = ("VENDINVOICETRANS", """
SELECT t.DATAAREAID AS data_area, t.INVOICEID AS invoice_id,
       CONVERT(date, t.INVOICEDATE) AS invoice_date, t.LINENUM AS line_num,
       t.PURCHID AS purch_id, ISNULL(j.INVOICEACCOUNT, '') AS vendor_account,
       t.ITEMID AS item_id, t.NAME AS item_name, t.CURRENCYCODE AS currency,
       t.QTY AS qty, t.PURCHPRICE AS purch_price, t.LINEAMOUNT AS line_amount,
       t.LINEAMOUNTMST AS line_amount_mst, t.TAXAMOUNT AS tax_amount,
       t.DISCAMOUNT AS disc_amount, t.INVENTTRANSID AS invent_trans_id, t.RECID AS recid
FROM VENDINVOICETRANS t
OUTER APPLY (SELECT TOP 1 h.INVOICEACCOUNT FROM VENDINVOICEJOUR h
             WHERE h.DATAAREAID = t.DATAAREAID AND h.PURCHID = t.PURCHID
               AND h.INVOICEID = t.INVOICEID AND h.INVOICEDATE = t.INVOICEDATE
               AND h.NUMBERSEQUENCEGROUP = t.NUMBERSEQUENCEGROUP
               AND h.INTERNALINVOICEID = t.INTERNALINVOICEID) j
""")

TABLES["fact_vend_trans"] = ("VENDTRANS", """
SELECT t.DATAAREAID AS data_area, t.ACCOUNTNUM AS vendor_account,
       t.CURRENCYCODE AS currency, CONVERT(date, t.TRANSDATE) AS trans_date,
       CONVERT(date, t.DUEDATE) AS due_date, CONVERT(date, t.CLOSED) AS closed,
       t.TRANSTYPE AS trans_type, t.AMOUNTCUR AS amount_cur, t.AMOUNTMST AS amount_mst,
       t.SETTLEAMOUNTCUR AS settle_cur, t.SETTLEAMOUNTMST AS settle_mst,
       t.VOUCHER AS voucher, t.INVOICE AS invoice, t.TXT AS txt, t.RECID AS recid
FROM VENDTRANS t
""")

# INVENTTRANSORIGIN (by RECID) and INVENTDIM (by id + company) are both unique
# on the join key, so neither can fan the grain out.
TABLES["fact_invent_trans"] = ("INVENTTRANS", """
SELECT t.DATAAREAID AS data_area, t.ITEMID AS item_id,
       ISNULL(o.REFERENCECATEGORY, 0) AS ref_category, ISNULL(o.REFERENCEID, '') AS ref_id,
       t.STATUSISSUE AS status_issue, t.STATUSRECEIPT AS status_receipt,
       CONVERT(date, t.DATEPHYSICAL) AS date_physical,
       CONVERT(date, t.DATEFINANCIAL) AS date_financial,
       CONVERT(date, t.DATESTATUS) AS date_status, t.QTY AS qty,
       t.COSTAMOUNTPOSTED AS cost_amount_posted, t.COSTAMOUNTPHYSICAL AS cost_amount_physical,
       t.COSTAMOUNTADJUSTMENT AS cost_amount_adjustment,
       t.REVENUEAMOUNTPHYSICAL AS revenue_amount_physical,
       ISNULL(d.INVENTSITEID, '') AS site, ISNULL(d.INVENTLOCATIONID, '') AS warehouse,
       t.CURRENCYCODE AS currency, t.INVOICEID AS invoice_id, t.VOUCHER AS voucher,
       t.PROJID AS proj_id, ISNULL(o.INVENTTRANSID, '') AS invent_trans_id, t.RECID AS recid
FROM INVENTTRANS t
LEFT JOIN INVENTTRANSORIGIN o ON o.RECID = t.INVENTTRANSORIGIN
LEFT JOIN INVENTDIM d ON d.INVENTDIMID = t.INVENTDIMID AND d.DATAAREAID = t.DATAAREAID
""")

TABLES["fact_proj_posting"] = ("PROJTRANSPOSTING", """
SELECT t.DATAAREAID AS data_area, t.PROJID AS proj_id, t.PROJTRANSTYPE AS proj_trans_type,
       t.POSTINGTYPE AS posting_type, t.COSTSALES AS cost_sales,
       CONVERT(date, t.PROJTRANSDATE) AS trans_date,
       CONVERT(date, t.LEDGERTRANSDATE) AS ledger_date, t.AMOUNTMST AS amount_mst,
       t.QTY AS qty, t.CATEGORYID AS category_id, t.EMPLITEMID AS item_id,
       t.VOUCHER AS voucher, t.TRANSID AS trans_id, t.RECID AS recid
FROM PROJTRANSPOSTING t
""")

TABLES["fact_proj_item_trans"] = ("PROJITEMTRANS", """
SELECT t.DATAAREAID AS data_area, t.PROJID AS proj_id, t.ITEMID AS item_id,
       t.CATEGORYID AS category_id, CONVERT(date, t.TRANSDATE) AS trans_date,
       t.CURRENCYID AS currency, t.QTY AS qty, t.TOTALCOSTAMOUNTCUR AS cost_amount,
       t.TOTALSALESAMOUNTCUR AS sales_amount, t.TXT AS txt,
       t.PROJTRANSID AS proj_trans_id, t.RECID AS recid
FROM PROJITEMTRANS t
""")

# =============================================================================
# ClickHouse side
# =============================================================================


def ch(sql: str, body: bytes | None = None) -> str:
    if body is None:
        r = client.post(CH, content=sql.encode())
    else:
        r = client.post(CH, params={"query": sql}, content=body)
    if r.status_code != 200:
        raise RuntimeError(f"ClickHouse: {r.text.strip()}\n--- while running:\n{sql[:500]}")
    return r.text


def cell(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return "1970-01-01" if v.year < 1970 else v.isoformat()
    if isinstance(v, Decimal):
        return str(v)  # quoted, so no float rounding on the way in
    return v


def insert(table: str, cols: list[str], rows) -> None:
    body = b"\n".join(json.dumps({c: cell(v) for c, v in zip(cols, r)}).encode() for r in rows)
    ch(f"INSERT INTO {table} FORMAT JSONEachRow", body)


def run_sql_file(path: Path) -> None:
    """Statements end with ';' at end of line. Comment lines are dropped first,
    so a ';' inside a comment cannot split a statement."""
    text = "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("--"))
    for stmt in re.split(r";\s*$", text, flags=re.M):
        if stmt.strip():
            ch(stmt)


# =============================================================================
# SQL Server side
# =============================================================================


def connect():
    os.environ.setdefault("TDSVER", os.environ.get("MSSQL_TDS_VERSION", "7.0"))
    import pymssql

    return pymssql.connect(
        server=os.environ["MSSQL_HOST"],
        user=os.environ["MSSQL_USER"],
        password=os.environ["MSSQL_PASS"],
        database=os.environ.get("MSSQL_DB", "MicrosoftDynamicsAX"),
        port=os.environ.get("MSSQL_PORT", "1433"),
        login_timeout=30,
    )


def load_table(conn, table: str) -> int:
    base, sql = TABLES[table]
    cur = conn.cursor()
    staging = f"ax.{table}__load"
    ch(f"DROP TABLE IF EXISTS {staging}")
    ch(f"CREATE TABLE {staging} AS ax.{table}")
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    n = 0
    while rows := cur.fetchmany(CHUNK):
        insert(staging, cols, rows)
        n += len(rows)
    cur.execute(f"SELECT COUNT_BIG(*) FROM {base}")
    src = cur.fetchone()[0]
    if n != src:
        # Either the ERP wrote rows mid-load (harmless, next run catches up) or
        # a join fanned out / dropped rows (a bug in the SELECT above).
        print(f"  WARNING {table}: read {n:,} rows but {base} has {src:,}")
    ch(f"EXCHANGE TABLES {staging} AND ax.{table}")
    ch(f"DROP TABLE {staging}")
    return n


def load(tables: list[str]) -> None:
    unknown = set(tables) - set(TABLES)
    if unknown:
        sys.exit(f"unknown table(s): {', '.join(sorted(unknown))}; choose from {', '.join(TABLES)}")
    conn = connect()
    for table in tables:
        t0 = time.time()
        n = load_table(conn, table)
        secs = time.time() - t0
        ch("INSERT INTO ax.load_log (table_name, rows, seconds) "
           f"VALUES ('{table}', {n}, {secs:.1f})")
        print(f"{table:<24} {n:>10,} rows  {secs:6.1f}s", flush=True)
    conn.close()
    aggregates()


def aggregates() -> None:
    t0 = time.time()
    run_sql_file(HERE / "aggregates.sql")
    print(f"aggregates rebuilt  {time.time() - t0:.1f}s")


def init() -> None:
    run_sql_file(HERE / "ch_schema.sql")
    print("schema ready: " + ch("SELECT count() FROM system.tables WHERE database = 'ax'").strip()
          + " tables in ax")


def check() -> None:
    """Connectivity + the size of every source table, before loading anything."""
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT @@VERSION, DB_NAME()")
    ver, db = cur.fetchone()
    print(f"connected to {db} on {ver.splitlines()[0]}")
    for table, (base, _) in TABLES.items():
        cur.execute(f"SELECT COUNT_BIG(*) FROM {base}")
        print(f"  {base:<20} -> ax.{table:<24} {cur.fetchone()[0]:>10,} rows")
    conn.close()
    print("ClickHouse: " + ch("SELECT version()").strip())


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--check" in args:
        check()
    elif "--init" in args:
        init()
    elif "--aggregates" in args:
        aggregates()
    else:
        load(args or list(TABLES))
