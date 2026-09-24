#!/usr/bin/env python3
"""MSSQL schema -> schema.txt, the input to the ClickHouse DDL you write next.

    pip install pymssql
    MSSQL_HOST=... MSSQL_USER=... MSSQL_PASS=... MSSQL_DB=AxDB python3 schema_dump.py

Three queries: columns, primary keys, row counts. Everything you need to pick a
ClickHouse type per column, an ORDER BY per table, and which tables are big
enough to bother moving.

ponytail: pipe-delimited text, not JSON/YAML. You read this file, you don't parse
it. Switch to JSON the day something downstream consumes it programmatically.
"""
import os
import sys

import pymssql

OUT = sys.argv[1] if len(sys.argv) > 1 else "schema.txt"

COLUMNS = """
SELECT TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION, COLUMN_NAME, DATA_TYPE,
       CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE
FROM INFORMATION_SCHEMA.COLUMNS
ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
"""

KEYS = """
SELECT k.TABLE_SCHEMA, k.TABLE_NAME, k.ORDINAL_POSITION, k.COLUMN_NAME, c.CONSTRAINT_TYPE
FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS c
  ON c.CONSTRAINT_NAME = k.CONSTRAINT_NAME AND c.TABLE_SCHEMA = k.TABLE_SCHEMA
WHERE c.CONSTRAINT_TYPE IN ('PRIMARY KEY', 'UNIQUE')
ORDER BY k.TABLE_SCHEMA, k.TABLE_NAME, c.CONSTRAINT_TYPE, k.ORDINAL_POSITION
"""

# index_id < 2 = heap or clustered index, i.e. the real row count, counted once.
COUNTS = """
SELECT s.name, t.name, SUM(p.row_count)
FROM sys.dm_db_partition_stats p
JOIN sys.tables t ON t.object_id = p.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE p.index_id < 2
GROUP BY s.name, t.name
ORDER BY SUM(p.row_count) DESC
"""

SECTIONS = [
    ("COLUMNS  schema|table|pos|column|type|len|precision|scale|nullable", COLUMNS),
    ("KEYS  schema|table|pos|column|constraint", KEYS),
    ("ROW COUNTS  schema|table|rows", COUNTS),
]


def main():
    # ponytail: TDS 7.0 because this server only offers TLS 1.0 and the bundled
    # FreeTDS refuses it on 7.1+. Fine for reading INFORMATION_SCHEMA. Moving
    # actual DATA needs 7.3+ (datetime2, nvarchar(max)) — that means a FreeTDS
    # built against a seclevel=0 OpenSSL, or TLS 1.2 turned on server-side.
    os.environ.setdefault("TDSVER", "7.0")
    conn = pymssql.connect(
        server=os.environ["MSSQL_HOST"],
        user=os.environ["MSSQL_USER"],
        password=os.environ["MSSQL_PASS"],
        database=os.environ["MSSQL_DB"],
        port=os.environ.get("MSSQL_PORT", "1433"),
    )
    with conn, open(OUT, "w") as f:
        for header, sql in SECTIONS:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            f.write(f"\n=== {header}  ({len(rows)} rows) ===\n")
            for r in rows:
                f.write("|".join("" if v is None else str(v) for v in r) + "\n")
            print(f"{header.split()[0]}: {len(rows)} rows", file=sys.stderr)
    print(f"wrote {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
