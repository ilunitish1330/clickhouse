#!/usr/bin/env python3
"""End-to-end check of ax_load.py with SQL Server faked out.

    python3 test_ax_load.py        # needs ClickHouse on CH_URL; DROPS database ax

Runs the real load path (staging table, EXCHANGE, load_log, aggregates.sql)
against a fake cursor that returns rows shaped by each SELECT's column aliases
and the target table's types. Proves: every alias in ax_load.TABLES is a real
ClickHouse column, 1900 dates survive, a reload replaces instead of appending,
and the aggregates add up to the facts.
"""
import re
from datetime import date
from decimal import Decimal

import ax_load

ROWS = 3


def aliases(sql: str) -> list[str]:
    return re.findall(r"\bAS (\w+)", sql[:sql.rindex("\nFROM")])


def fake_rows(table: str, cols: list[str]) -> list[tuple]:
    types = dict(line.split("\t") for line in ax_load.ch(
        f"SELECT name, type FROM system.columns WHERE database = 'ax' AND table = '{table}'"
        " FORMAT TSV").strip().splitlines())
    missing = [c for c in cols if c not in types]
    assert not missing, f"{table}: SELECT aliases not in ClickHouse table: {missing}"

    def val(col, i):
        t = types[col]
        if col == "data_area":
            return "usmf"
        if t.startswith("Decimal"):
            return Decimal("-12.5") if col == "amount_mst" and i == 0 else Decimal("12.5")
        if t == "Date":
            return date(1900, 1, 1) if i == 0 else date(2024, 3, 15)  # AX's empty date
        if "Int" in t:
            return 1 if col.startswith("status_") else i + 1
        return f"{col}_{i}"

    return [tuple(val(c, i) for c in cols) for i in range(ROWS)]


class FakeCursor:
    def execute(self, sql):
        if sql.startswith("SELECT COUNT_BIG"):
            self.rows, self.description = [(ROWS,)], [("n",)]
            return
        table = next(t for t, (_, s) in ax_load.TABLES.items() if s == sql)
        cols = aliases(sql)
        self.description = [(c,) for c in cols]
        self.rows = fake_rows(table, cols)

    def fetchmany(self, n):
        out, self.rows = self.rows[:n], self.rows[n:]
        return out

    def fetchone(self):
        return self.rows.pop(0)


class FakeConn:
    def cursor(self):
        return FakeCursor()

    def close(self):
        pass


def q(sql: str) -> str:
    return ax_load.ch(sql).strip()


def main() -> None:
    ax_load.ch("DROP DATABASE IF EXISTS ax")
    ax_load.init()
    ax_load.connect = lambda: FakeConn()
    ax_load.load(list(ax_load.TABLES))
    ax_load.load(list(ax_load.TABLES))  # second run must replace, not append

    for t in ax_load.TABLES:
        n = int(q(f"SELECT count() FROM ax.{t}"))
        assert n == ROWS, f"{t}: {n} rows after two loads, want {ROWS}"
    assert q("SELECT count() FROM system.tables WHERE database='ax' AND endsWith(name, '__load')") == "0"

    # 1900-01-01 lands on Date's floor, and open-item logic reads it as "open"
    assert q("SELECT min(trans_date) FROM ax.fact_cust_trans") == "1970-01-01"
    assert q("SELECT sum(is_open) FROM ax.fact_cust_trans") == "1"

    # DEFAULT columns are computed on insert and returned by SELECT *
    # line_amount 12.5 - cost_price 12.5 * qty 12.5 = -143.75
    assert q("SELECT DISTINCT margin FROM ax.fact_sales") == "-143.75"
    assert "margin" in q("SELECT * FROM ax.fact_sales LIMIT 1 FORMAT TSVWithNames").split("\n")[0]

    # aggregates add up to their facts
    for agg, a, fact, f in [
        ("agg_sales_monthly", "revenue", "fact_sales", "line_amount"),
        ("agg_purchases_monthly", "purchase_amount_mst", "fact_vend_invoice_line", "line_amount_mst"),
        ("agg_project_monthly", "amount_mst", "fact_proj_posting", "amount_mst"),
        ("agg_inventory_monthly", "net_qty", "fact_invent_trans", "qty"),
        ("agg_ar_monthly", "net_change", "fact_cust_trans", "amount_mst"),
    ]:
        x, y = q(f"SELECT sum({a}) FROM ax.{agg}"), q(f"SELECT sum({f}) FROM ax.{fact}")
        assert x == y, f"{agg}.{a} = {x}, {fact}.{f} = {y}"

    # Power BI cannot read Decimal128 or AggregateFunction columns
    wide = q("SELECT groupArray(concat(table, '.', name)) FROM system.columns WHERE database = 'ax'"
             " AND (type LIKE 'Decimal(3%' OR type LIKE 'Decimal128%' OR type LIKE '%AggregateFunction%')")
    assert wide == "[]", wide

    # every status code in the facts joins to dim_status
    orphans = q("SELECT count() FROM ax.fact_sales f LEFT ANTI JOIN ax.dim_status s"
                " ON s.status_type = 'sales_status' AND s.status_code = f.sales_status")
    assert orphans == "0", orphans
    assert q("SELECT count() FROM ax.dim_status WHERE (status_type, status_code) = ('sales_status', 1)") == "1"

    # Power BI single-column keys line up between facts, aggregates and dims
    assert q("SELECT customer_key FROM ax.fact_sales ORDER BY recid LIMIT 1") == "usmf|cust_account_0"
    assert q("SELECT count() FROM ax.agg_sales_monthly a JOIN ax.dim_item d ON d.item_key = a.item_key"
             " WHERE d.item_id = 'item_id_0'") == "1"

    assert int(q("SELECT count() FROM ax.v_last_load")) == len(ax_load.TABLES)
    print(q("SELECT agg_table, fact_rows, agg_rows FROM ax.v_aggregate FORMAT PrettyCompactNoEscapes"))
    ax_load.ch("DROP DATABASE ax")
    print("ok")


if __name__ == "__main__":
    main()
