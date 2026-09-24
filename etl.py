#!/usr/bin/env python3
"""MSSQL (Dynamics) -> ClickHouse, incremental, one wide table.

The join lives in a SQL Server VIEW (see VIEW_SQL below) — that's the transform.
This script only moves rows: read everything newer than what ClickHouse already
has, POST it in chunks. Re-reading a changed row is fine, ReplacingMergeTree
collapses it on the version column.

    pip install pyodbc            # + msodbcsql18 driver
    python3 etl.py                # incremental load
    python3 etl.py --demo         # self-check, no MSSQL needed

ponytail: no Airflow/dbt/Debezium. A watermark query and a loop is the whole
scheduler. Move to CDC (see bottom) when you need deletes or sub-minute lag.
"""
import json
import os
import sys
from datetime import datetime

import httpx

CH = os.environ.get("CH_URL", "http://localhost:8123/")
DSN = os.environ.get(
    "MSSQL_DSN",
    "DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=AxDB;"
    "UID=sa;PWD=changeme;TrustServerCertificate=yes",
)
CHUNK = 50_000

# --- the transform: one row per customer, all 6 tables folded in ------------
# Dynamics 365 F&O / AX flavour. For Dataverse (CE) the same shape is
# contact + account + customeraddress + ... — swap the table names, keep the pattern.
# 1:1 relations become columns. 1:N (addresses, contacts) becomes ONE row via
# an aggregate — pick the primary, or count them. Never fan the grain out.
VIEW_SQL = """
CREATE OR ALTER VIEW dbo.v_customer_flat AS
SELECT
    c.DATAAREAID                                   AS data_area,
    c.ACCOUNTNUM                                   AS customer_id,
    p.NAME                                         AS name,
    p.NAMEALIAS                                    AS name_alias,
    c.CUSTGROUP                                    AS group_code,
    g.NAME                                         AS group_name,
    c.CURRENCY                                     AS currency,
    c.CREDITMAX                                    AS credit_limit,
    c.BLOCKED                                      AS blocked,
    addr.ADDRESS                                   AS address,
    addr.CITY                                      AS city,
    addr.COUNTRYREGIONID                           AS country,
    email.LOCATOR                                  AS email,
    phone.LOCATOR                                  AS phone,
    -- the watermark: newest change anywhere in the joined set, or the loader
    -- misses edits made to a child row only.
    (SELECT MAX(v) FROM (VALUES (c.MODIFIEDDATETIME), (p.MODIFIEDDATETIME),
        (addr.MODIFIEDDATETIME), (email.MODIFIEDDATETIME),
        (phone.MODIFIEDDATETIME)) AS t(v))         AS modified
FROM CUSTTABLE c
JOIN DIRPARTYTABLE p            ON p.RECID = c.PARTY
LEFT JOIN CUSTGROUP g           ON g.CUSTGROUP = c.CUSTGROUP AND g.DATAAREAID = c.DATAAREAID
LEFT JOIN LOGISTICSPOSTALADDRESS addr    ON addr.RECID = (
    SELECT TOP 1 pl.LOCATION FROM DIRPARTYLOCATION pl
    WHERE pl.PARTY = c.PARTY AND pl.ISPRIMARY = 1)
LEFT JOIN LOGISTICSELECTRONICADDRESS email ON email.RECID = p.PRIMARYCONTACTEMAIL
LEFT JOIN LOGISTICSELECTRONICADDRESS phone ON phone.RECID = p.PRIMARYCONTACTPHONE
"""

TARGET = """
CREATE DATABASE IF NOT EXISTS dw;
CREATE TABLE IF NOT EXISTS {table}
(
    data_area    LowCardinality(String),
    customer_id  String,
    name         String,
    name_alias   String,
    group_code   LowCardinality(String),
    group_name   String,
    currency     LowCardinality(String),
    credit_limit Decimal(18, 2),
    blocked      UInt8,
    address      String,
    city         String,
    country      LowCardinality(String),
    email        String,
    phone        String,
    modified     DateTime64(3)
)
ENGINE = ReplacingMergeTree(modified)
ORDER BY (data_area, customer_id)
"""

# Only rows changed since the last run. > not >=: the watermark row is already in.
DELTA_SQL = "SELECT * FROM dbo.v_customer_flat WHERE modified > ? ORDER BY modified"

client = httpx.Client(timeout=120.0)


def ch(sql: str, body: bytes = b"") -> str:
    r = client.post(CH, params={"query": sql}, content=body)
    r.raise_for_status()
    return r.text


def watermark() -> datetime:
    """Where the last run stopped — kept in the target table, not a state file.
    ClickHouse is the only thing that knows what it actually holds."""
    ts = ch("SELECT max(modified) FROM dw.customer").strip()
    return datetime.fromisoformat(ts) if not ts.startswith("1970") else datetime(1900, 1, 1)


def insert(rows: list[dict], table: str = "dw.customer") -> None:
    body = b"\n".join(json.dumps(r, default=str).encode() for r in rows)
    ch(f"INSERT INTO {table} FORMAT JSONEachRow", body)


def create(table: str = "dw.customer") -> None:
    for stmt in TARGET.format(table=table).strip().split(";"):
        if stmt.strip():
            ch(stmt)


def load() -> int:
    import pyodbc

    create()
    since = watermark()
    print(f"loading rows modified after {since}")
    cur = pyodbc.connect(DSN).cursor().execute(DELTA_SQL, since)
    cols = [c[0] for c in cur.description]
    total = 0
    while batch := cur.fetchmany(CHUNK):
        insert([dict(zip(cols, row)) for row in batch])
        total += len(batch)
        print(f"  {total:,}", flush=True)
    return total


def demo() -> None:
    """Runs the ClickHouse half against a scratch table — proves the insert path
    and that a re-loaded row replaces rather than duplicates."""
    T = "dw.customer_demo"
    create(T)
    ch(f"TRUNCATE TABLE {T}")

    def row(name, modified):
        return {"data_area": "usmf", "customer_id": "C1", "name": name, "name_alias": "",
                "group_code": "10", "group_name": "Retail", "currency": "USD",
                "credit_limit": "1000.00", "blocked": 0, "address": "1 Main St",
                "city": "Redmond", "country": "US", "email": "a@b.c", "phone": "",
                "modified": modified}

    insert([row("Contoso", "2024-01-01 10:00:00.000")], T)
    insert([row("Contoso Ltd", "2024-01-02 10:00:00.000")], T)  # same key, later version
    n = int(ch(f"SELECT count() FROM {T}").strip())
    assert n == 2, f"both versions should land as separate parts, got {n}"
    name, ver = ch(f"SELECT name, max(modified) FROM {T} FINAL GROUP BY name").strip().split("\t")
    assert name == "Contoso Ltd", f"FINAL should keep the newest version, got {name}"
    assert ver.startswith("2024-01-02"), ver
    wm = ch(f"SELECT max(modified) FROM {T}").strip()
    assert wm.startswith("2024-01-02"), f"watermark must be the newest row, got {wm}"
    ch(f"DROP TABLE {T}")
    print("ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    elif "--view" in sys.argv:
        print(VIEW_SQL)
    else:
        print(f"{load():,} rows")
