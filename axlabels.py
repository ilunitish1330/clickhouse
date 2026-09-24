#!/usr/bin/env python3
"""Add AX table labels ("CUSTTABLE" -> "Customers") to schema_full.txt, in place.

    MSSQL_HOST=... MSSQL_USER=... MSSQL_PASS=... python3 axlabels.py

Labels live in the model store DB (MicrosoftDynamicsAX_model), not the business
DB: each AOT element's Properties blob is UTF-16 with label refs like @SYS16445,
resolved through ModelElementLabel.

ponytail: TABLE labels only. AX fields mostly have no Label of their own — they
inherit it from their Extended Data Type, and the first token in a field blob is
as often HelpText as Label. Getting field labels right means decoding the
property-tag layout of the blob; do that only if column names alone prove
unreadable.
"""
import os
import re
import sys

import pymssql

FILE = sys.argv[1] if len(sys.argv) > 1 else "schema_full.txt"
MODEL_DB = os.environ.get("MSSQL_MODEL_DB", "MicrosoftDynamicsAX_model")
LANG = os.environ.get("AX_LANG", "en_us")
TOKEN = re.compile(rb"@([A-Za-z]{2,6})(\d+)")  # @SYS16445 -> module SYS, id 16445
HEADER = re.compile(r"^-- (\S+?)\.(\S+?): (.*)$")


def table_labels():
    conn = pymssql.connect(
        server=os.environ["MSSQL_HOST"],
        user=os.environ["MSSQL_USER"],
        password=os.environ["MSSQL_PASS"],
        database=MODEL_DB,
        port=os.environ.get("MSSQL_PORT", "1433"),
    )
    cur = conn.cursor()
    cur.execute("SELECT Module, LabelId, Text FROM ModelElementLabel WHERE Language=%s", (LANG,))
    labels = {}
    for module, lid, text in cur.fetchall():
        labels.setdefault((module.strip().upper(), int(lid)), text)

    # ElementType 44 = Table. Several rows per table (one per AOT layer) — first wins.
    cur.execute(
        "SELECT e.Name, d.Properties FROM ModelElement e "
        "JOIN ModelElementData d ON d.ElementHandle = e.ElementHandle WHERE e.ElementType = 44"
    )
    out = {}
    for name, props in cur.fetchall():
        # The blob is UTF-16LE but not 2-byte aligned from the start, so strip the
        # padding nulls and match on bytes instead of decoding.
        m = TOKEN.search(bytes(props).replace(b"\x00", b""))
        if not m:
            continue
        text = labels.get((m.group(1).upper().decode(), int(m.group(2))))
        if text:
            out.setdefault(name.upper(), text)
    conn.close()
    return out


def main():
    os.environ.setdefault("TDSVER", "7.0")  # see schema_dump.py
    labels = table_labels()
    print(f"{len(labels)} table labels", file=sys.stderr)

    with open(FILE) as f:
        lines = f.readlines()
    hits = 0
    for i, line in enumerate(lines):
        m = HEADER.match(line.rstrip("\n"))
        if m and m.group(2).upper() in labels:
            lines[i] = f"-- {m.group(1)}.{m.group(2)}: {m.group(3)}  |  {labels[m.group(2).upper()]}\n"
            hits += 1
    with open(FILE, "w") as f:
        f.writelines(lines)
    print(f"labelled {hits} of {sum(1 for l in lines if HEADER.match(l.rstrip()))} tables in {FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
