#!/usr/bin/env python3
"""Find the SQL Server table / view / procedure behind the Statistic Report.

    python3 find_source.py

Uses the SQL Server login in .env and searches EVERY database that login can
open for (1) tables and views having the report's distinctive columns and
(2) stored procedures / views whose code mentions them. Read-only.
"""
import os

os.environ["MSSQL_DB"] = "master"  # before ax_load reads .env: start from master
import ax_load  # noqa: E402

MARKERS = ["TARIFFGROUPCODE", "MCSEXTERNALASSETID", "STATCATEGORIES", "ADDITIONALITEMSTYPE",
           "CURDATETICKS", "METERWATERNODE", "INVOICEVALUETYPE"]
IN_LIST = ", ".join(f"'{m}'" for m in MARKERS)


def main() -> None:
    conn = ax_load.connect()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sys.databases WHERE HAS_DBACCESS(name) = 1 AND state = 0 ORDER BY name")
    dbs = [r[0] for r in cur.fetchall()]
    print(f"{len(dbs)} databases this login can open: {', '.join(dbs)}\n")
    found = False
    for db in dbs:
        try:
            cur.execute(f"""
                SELECT c.TABLE_SCHEMA, c.TABLE_NAME, t.TABLE_TYPE, COUNT(DISTINCT c.COLUMN_NAME)
                FROM [{db}].INFORMATION_SCHEMA.COLUMNS c
                JOIN [{db}].INFORMATION_SCHEMA.TABLES t
                  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
                WHERE c.COLUMN_NAME IN ({IN_LIST})
                GROUP BY c.TABLE_SCHEMA, c.TABLE_NAME, t.TABLE_TYPE
                HAVING COUNT(DISTINCT c.COLUMN_NAME) >= 2
                ORDER BY 4 DESC""")
            for schema, table, kind, hits in cur.fetchall():
                found = True
                cur2 = conn.cursor()
                cur2.execute(f"SELECT COUNT_BIG(*) FROM [{db}].[{schema}].[{table}]")
                print(f"  {kind:<10} [{db}].[{schema}].[{table}]  "
                      f"{hits}/{len(MARKERS)} marker columns, {cur2.fetchone()[0]:,} rows")
            cur.execute(f"""
                SELECT s.name, o.name, o.type_desc
                FROM [{db}].sys.sql_modules m
                JOIN [{db}].sys.objects o ON o.object_id = m.object_id
                JOIN [{db}].sys.schemas s ON s.schema_id = o.schema_id
                WHERE m.definition LIKE '%TARIFFGROUPCODE%' OR m.definition LIKE '%MCSEXTERNALASSETID%'""")
            for schema, name, kind in cur.fetchall():
                found = True
                print(f"  {kind:<10} [{db}].[{schema}].[{name}]  (its code mentions the report columns)")
        except Exception as e:  # a database we can list but not read
            print(f"  (skipped {db}: {str(e).splitlines()[0][:100]})")
    print("\nfound candidates above" if found else
          "\nnothing found: the report comes from another server, or this login cannot see it")
    conn.close()


if __name__ == "__main__":
    main()
