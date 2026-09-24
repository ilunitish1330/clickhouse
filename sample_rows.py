#!/usr/bin/env python3
"""One sample row per non-empty table -> samples.txt.

    MSSQL_HOST=... MSSQL_USER=... MSSQL_PASS=... MSSQL_DB=... python3 sample_rows.py

Reads tables.txt (from mkddl/awk) for the table list, skips the empty ones, and
writes column: value pairs so you can see what the data actually looks like
before choosing ClickHouse types. Types lie; values don't.

ponytail: TOP 1 with no ORDER BY — an arbitrary row, not a representative one.
Good enough to see shapes and spot empty-string-vs-NULL. Use TABLESAMPLE or a
few rows per table when you need to judge cardinality.
"""
import os
import sys

import pymssql

TABLES = sys.argv[1] if len(sys.argv) > 1 else "tables.txt"
OUT = sys.argv[2] if len(sys.argv) > 2 else "samples.txt"
MAXLEN = 200  # long blobs/XML tell you nothing past this


def main():
    os.environ.setdefault("TDSVER", "7.0")  # see schema_dump.py
    conn = pymssql.connect(
        server=os.environ["MSSQL_HOST"],
        user=os.environ["MSSQL_USER"],
        password=os.environ["MSSQL_PASS"],
        database=os.environ["MSSQL_DB"],
        port=os.environ.get("MSSQL_PORT", "1433"),
    )
    with open(TABLES) as f:
        targets = [
            line.split("|") for line in f if line.count("|") == 2 and line.rsplit("|", 1)[1].strip().isdigit()
        ]
    targets = [(t, int(r)) for t, _cols, r in targets if int(r) > 0]

    done = failed = 0
    with conn, open(OUT, "w") as out:
        for table, rows in targets:
            schema, name = table.split(".", 1)
            cur = conn.cursor()
            try:
                cur.execute(f"SELECT TOP 1 * FROM [{schema}].[{name}]")
                row = cur.fetchone()
            except Exception as e:  # permissions, weird types — keep going
                out.write(f"\n=== {table}  ERROR: {str(e)[:120]}\n")
                failed += 1
                continue
            cols = [d[0] for d in cur.description]
            out.write(f"\n=== {table}  ({rows} rows)\n")
            for col, val in zip(cols, row or []):
                s = "NULL" if val is None else str(val)
                if len(s) > MAXLEN:
                    s = s[:MAXLEN] + f"...<{len(s)} chars>"
                out.write(f"    {col}: {s}\n")
            done += 1
            if done % 250 == 0:
                print(f"{done}/{len(targets)}", file=sys.stderr)
    print(f"wrote {OUT}: {done} sampled, {failed} failed", file=sys.stderr)


if __name__ == "__main__":
    main()
