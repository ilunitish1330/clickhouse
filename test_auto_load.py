#!/usr/bin/env python3
"""Checks auto_load.py end to end: .xlsx, CSV, SQL (faked), inbox, model reuse.

    python3 test_auto_load.py      # needs ClickHouse on CH_URL and openpyxl;
                                   # drops databases t_sales, t_sql, t_inbox
"""
import json
import random
import shutil
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook

import auto_load
import ax_load

random.seed(7)
TMP = Path(tempfile.mkdtemp())


def q(sql: str) -> str:
    return ax_load.ch(sql).strip()


def sales_rows(n: int, start: datetime):
    """Invoice lines: electricity (kWh, ~5/unit) and water (m3, ~30/unit)."""
    regions = {"1": "MAHE", "2": "PRASLIN", "3": "LA DIGUE"}
    for i in range(n):
        util = random.choice(["E", "W"])
        qty = random.randint(1, 500)
        price = 5 if util == "E" else 30
        r = random.choice(list(regions))
        yield [start + timedelta(days=random.randint(0, 27)), f"INV-{start:%y%m}-{i // 3:06d}",
               f"CUS-{random.randint(1, 1500):05d}", r, regions[r], util,
               {"E": "Electricity", "W": "Water"}[util], qty, round(qty * price * random.uniform(0.95, 1.05), 2),
               price, None, "PUC"]


HEADER = ["Invoice Date", "Invoice Id", "Customer Id", "Region", "Region Name", "Utility Type",
          "Utility Name", "Quantity", "Amount", "Unit Price", "Notes", "Company"]


def write_xlsx(path: Path, rows) -> float:
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    total = 0.0
    for r in rows:
        ws.append(r)
        total += r[8]
    wb.save(path)
    return round(total, 2)


def main() -> None:
    for db in ("t_sales", "t_sql", "t_inbox"):
        q(f"DROP DATABASE IF EXISTS {db}")
        (auto_load.MODELS / f"{db}.json").unlink(missing_ok=True)

    # --- 1. xlsx with real dates: the model is decided from the data ----------
    jan = TMP / "sales_2025_01.xlsx"
    t1 = write_xlsx(jan, list(sales_rows(3000, datetime(2025, 1, 1))))
    auto_load.load_file("t_sales", jan)
    m = json.loads((auto_load.MODELS / "t_sales.json").read_text())
    role = lambda c: m["columns"][c]["role"]
    assert m["date"] == "invoice_date" and m["grain"] == "day", (m["date"], m["grain"])  # one month -> daily
    assert {x["name"] for x in m["measures"]} == {"quantity", "amount", "unit_price"}, m["measures"]
    assert not next(x for x in m["measures"] if x["name"] == "unit_price")["additive"]  # a price is averaged
    assert role("customer_id") == "entity" and role("invoice_id") == "document"
    assert role("notes") == "drop" and role("company") == "drop"
    dims = {d["key"]: d["attributes"] for d in m["dimensions"]}
    assert dims["region"] == ["region_name"] and dims["utility_type"] == ["utility_name"], dims
    unit = next(x for x in m["measures"] if x["name"] == "quantity")["split_by"]
    assert unit[0] == "utility_type", unit  # kWh vs m3 found from price per unit
    assert q("SELECT round(sum(amount), 2) FROM t_sales.fact") == f"{t1}"
    assert q("SELECT round(sum(amount), 2) FROM t_sales.agg_daily") == f"{t1}"
    assert q("SELECT round(sum(amount), 2) FROM t_sales.agg_customer_id_daily") == f"{t1}"
    assert "utility_type" in q("SELECT groupArray(name) FROM system.columns "
                               "WHERE database='t_sales' AND table='agg_customer_id_daily'")
    assert q("SELECT min(invoice_date) FROM t_sales.fact") == "2025-01-01"

    # --- 2. next month's file reuses the model; same file again replaces ------
    created = m["created"]
    feb = TMP / "sales_2025_02.xlsx"
    t2 = write_xlsx(feb, list(sales_rows(2500, datetime(2025, 2, 1))))
    auto_load.load_file("t_sales", feb)
    auto_load.load_file("t_sales", feb)  # sent twice: must not double
    m2 = json.loads((auto_load.MODELS / "t_sales.json").read_text())
    assert m2["created"] == created, "model must be reused, not re-decided"
    assert q("SELECT count() FROM t_sales.fact") == "5500"
    assert q("SELECT round(sum(amount), 2) FROM t_sales.agg_daily") == f"{round(t1 + t2, 2)}"

    # --- 3. a SQL query (SQL Server faked): values of every type ---------------
    class Cur:
        description = [("TRANSDATE",), ("ACCOUNTNUM",), ("AMOUNTMST",), ("CURRENCYCODE",)]

        def __init__(self):
            self.rows = [(datetime(2024, 1 + i % 12, 1 + i % 28, 13, 5), f"C{i % 1200:05d}",
                          Decimal(f"{(i % 997) - 300}.25"), random.choice(["USD", "EUR", None]))
                         for i in range(5000)]

        def execute(self, sql):
            pass

        def fetchmany(self, n):
            out, self.rows = self.rows[:n], self.rows[n:]
            return out

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass

    ax_load.connect = lambda: Conn()
    auto_load.load_sql("t_sql", "SELECT TRANSDATE, ACCOUNTNUM, AMOUNTMST, CURRENCYCODE FROM CUSTTRANS", None)
    auto_load.load_sql("t_sql", "SELECT ...", None)  # a query run replaces everything
    exp = float(sum(Decimal(f"{(i % 997) - 300}.25") for i in range(5000)))
    assert q("SELECT count() FROM t_sql.fact") == "5000"
    assert float(q("SELECT sum(amountmst) FROM t_sql.agg_monthly")) == exp
    assert q("SELECT count() FROM t_sql.agg_monthly WHERE period = '2024-03-01'") != "0"

    # --- 4. the inbox: a subfolder names the dataset; bad files are parked ------
    inbox = TMP / "inbox"
    (inbox / "t_inbox").mkdir(parents=True)
    rows = list(sales_rows(1200, datetime(2025, 3, 1)))
    with open(inbox / "t_inbox" / "march.csv", "w") as f:
        f.write(",".join(HEADER) + "\n")
        for r in rows:
            f.write(",".join("" if v is None else (v.strftime("%d/%m/%Y") if isinstance(v, datetime) else str(v))
                             for v in r) + "\n")
    (inbox / "t_inbox" / "broken.xls").write_bytes(b"not a real workbook")
    (inbox / "t_inbox" / "broken.xls").rename(inbox / "t_inbox" / "broken.zip")
    auto_load.run_inbox(inbox)
    assert not list((inbox / "t_inbox").iterdir()), "inbox must be empty after a run"
    assert len(list((inbox / "done" / "t_inbox").iterdir())) == 1
    assert len(list((inbox / "failed").glob("*.error.txt"))) == 1
    assert q("SELECT count() FROM t_inbox.fact") == "1200"
    assert q("SELECT min(invoice_date) FROM t_inbox.fact") == "2025-03-01"  # dd/mm/yyyy read right

    # --- 5. never touch a database auto_load did not create -----------------------
    try:
        auto_load.ensure_db("ax")
        raise AssertionError("must refuse a reserved database")
    except SystemExit:
        pass

    for db in ("t_sales", "t_sql", "t_inbox"):
        q(f"DROP DATABASE {db}")
        for ext in ("json", "md"):
            (auto_load.MODELS / f"{db}.{ext}").unlink(missing_ok=True)
    shutil.rmtree(TMP)
    print("ok")


if __name__ == "__main__":
    main()
