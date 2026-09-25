"""The ten dashboards: every tile and chart as a metric over a ClickHouse table.

Metrics mirror the KPI catalogue and the Power BI measures (powerbi/build_pbip.py).
A page query always runs through scope_where(), which applies the user's role
and island BEFORE the user's own filters -- a filter can narrow what a user sees,
never widen it.
"""
import json
import re

import ax_load  # ch()

ONE_UNIT_B = "uniqExact(unit) = 1"
ONE_UNIT_U = "uniqExact(utility_code) = 1"

# name: (label, source, SQL expression, format)
METRICS = {
    "revenue": ("Total revenue", "b", "sum(amount)", "money"),
    "consumption": ("Total consumption", "b", f"if({ONE_UNIT_B}, sum(quantity), NULL)", "qty"),
    "rate": ("Revenue per unit", "b", f"if({ONE_UNIT_B}, sum(amount) / nullIf(sum(quantity), 0), NULL)", "rate"),
    "customers": ("Customers", "b", "uniqExact(customer_id)", "count"),
    "connections": ("Connections", "b", "uniqExact(connection_id)", "count"),
    "meters": ("Meters", "b", "uniqExactIf(meter_id, has_meter = 1)", "count"),
    "invoices": ("Invoices", "b", "uniqExact(invoice_id)", "count"),
    "lines": ("Billing lines", "b", "count()", "count"),
    "rev_per_customer": ("Revenue per customer", "b", "sum(amount) / nullIf(uniqExact(customer_id), 0)", "money"),
    "rev_per_connection": ("Revenue per connection", "b", "sum(amount) / nullIf(uniqExact(connection_id), 0)", "money"),
    "rev_per_invoice": ("Revenue per invoice", "b", "sum(amount) / nullIf(uniqExact(invoice_id), 0)", "money"),
    "cons_per_customer": ("Consumption per customer", "b",
                          f"if({ONE_UNIT_B}, sum(quantity) / nullIf(uniqExact(customer_id), 0), NULL)", "qty"),
    "cons_per_connection": ("Consumption per connection", "b",
                            f"if({ONE_UNIT_B}, sum(quantity) / nullIf(uniqExact(connection_id), 0), NULL)", "qty"),
    "cons_item_revenue": ("Consumption charges", "b", "sum(consumption_amount)", "money"),
    "avg_cons_bill": ("Consumption charge per unit", "b",
                      f"if({ONE_UNIT_B}, sum(consumption_amount) / nullIf(sum(consumption_qty), 0), NULL)", "rate"),
    "domestic_revenue": ("Domestic revenue", "b", "sumIf(amount, sector_type = 'Domestic')", "money"),
    "commercial_revenue": ("Commercial revenue", "b", "sumIf(amount, sector_type = 'Commercial')", "money"),
    "government_revenue": ("Government revenue", "b", "sumIf(amount, sector_type = 'Government')", "money"),
    "adjustment": ("Adjustments", "b", "sum(adjustment_amount)", "money"),
    "adjustment_rate": ("Adjustment rate", "b", "abs(sum(adjustment_amount)) / nullIf(abs(sum(amount)), 0)", "pct"),
    "credits": ("Credits and reversals", "b", "sum(credit_amount)", "money"),
    "credit_share": ("Credits as share of revenue", "b", "abs(sum(credit_amount)) / nullIf(abs(sum(amount)), 0)", "pct"),
    "free_text_invoices": ("Free-text invoices", "b", "uniqExactIf(invoice_id, is_free_text = 1)", "count"),
    "no_meter_connections": ("Connections without a meter", "b", "uniqExactIf(connection_id, has_meter = 0)", "count"),
    "pv_customers": ("PV customers", "b", "uniqExactIf(customer_id, pv_connection = 1)", "count"),
    "pv_connections": ("PV connections", "b", "uniqExactIf(connection_id, pv_connection = 1)", "count"),
    "pv_billing": ("PV billing", "b", "sumIf(amount, is_pv = 1)", "money"),
    "pv_share": ("PV share of revenue", "b", "sumIf(amount, is_pv = 1) / nullIf(sum(amount), 0)", "pct"),
    "cp_revenue": ("Revenue", "cp", "sum(amount)", "money"),
    "cp_customers": ("Customers", "cp", "uniqExact(customer_id)", "count"),
    "cp_consumption": ("Consumption", "cp", f"if({ONE_UNIT_U}, sum(consumption_qty), NULL)", "qty"),
    "cp_invoices": ("Invoices", "cp", "sum(invoices)", "count"),
    "cn_connections": ("Connections billed", "cn", "count()", "count"),
    "cn_avg_use": ("Average use per connection", "cn", f"if({ONE_UNIT_U}, avg(consumption_qty), NULL)", "qty"),
    "cn_zero": ("Zero-consumption connections", "cn", "countIf(consumption_qty <= 0)", "count"),
    "cn_jumped": ("Jumped 3x or more", "cn", "countIf(change_flag = 'Jumped 3x or more')", "count"),
    "cn_dropped": ("Dropped to zero", "cn", "countIf(change_flag = 'Dropped to zero')", "count"),
}
UNIT = {"b": f"if({ONE_UNIT_B}, any(unit), '')",
        "cp": f"if({ONE_UNIT_U}, if(any(utility_code) = 1, 'kWh', 'm3'), '')",
        "cn": f"if({ONE_UNIT_U}, if(any(utility_code) = 1, 'kWh', 'm3'), '')"}


def tile(metric, delta=False):
    return {"metric": metric, "delta": delta}


def chart(kind, title, dim=None, metrics=(), *, series=None, limit=None, sort="value", all_periods=False,
          wide=False, share=(), note=None, account_col=False, one_unit=False):
    """one_unit: the chart only makes sense for one unit (kWh or m3) even though
    its measure is a count -- e.g. consumption bands."""
    return {"kind": kind, "title": title, "dim": dim, "metrics": list(metrics), "series": series,
            "limit": limit, "sort": sort, "all_periods": all_periods, "wide": wide, "share": list(share),
            "note": note, "account_col": account_col, "one_unit": one_unit}


PAGES = {
    "executive": {
        "title": "Executive Overview", "icon": "grid", "utility": None,
        "subtitle": "Revenue, customers and consumption across every utility and island",
        "tiles": [tile("revenue", True), tile("customers", True), tile("connections"), tile("invoices"),
                  tile("rev_per_customer"), tile("meters")],
        "charts": [
            chart("col", "Revenue by utility", "utility_name", ["revenue"]),
            chart("grouped", "Revenue by island and utility", "region_name", ["revenue"], series="utility_name"),
            chart("bar", "Revenue by customer sector", "sector_type", ["revenue"]),
            chart("col", "Revenue by billing period", "period_label", ["revenue"], sort="dim", all_periods=True),
            chart("table", "Utilities at a glance", "utility_name",
                  ["revenue", "consumption", "rate", "customers", "invoices"], wide=True),
        ]},
    "electricity": {
        "title": "Electricity", "icon": "bolt", "utility": 1,
        "subtitle": "Electricity billing, kWh and tariffs",
        "tiles": [tile("revenue", True), tile("consumption", True), tile("rate"), tile("customers", True),
                  tile("rev_per_customer"), tile("cons_per_customer")],
        "charts": [
            chart("bar", "Revenue by island", "region_name", ["revenue"]),
            chart("bar", "kWh by sector", "sector_desc", ["consumption"]),
            chart("bar", "Revenue by tariff group", "tariff_desc", ["revenue"], limit=12),
            chart("col", "Revenue by billing period", "period_label", ["revenue"], sort="dim", all_periods=True),
            chart("table", "By billing category", "category_desc",
                  ["revenue", "consumption", "rate", "invoices", "rev_per_invoice"], wide=True),
        ]},
    "water": {
        "title": "Water", "icon": "drop", "utility": 3,
        "subtitle": "Water billing, m³ and tariffs",
        "tiles": [tile("revenue", True), tile("consumption", True), tile("rate"), tile("customers", True),
                  tile("cons_item_revenue"), tile("avg_cons_bill")],
        "charts": [
            chart("bar", "Revenue by island", "region_name", ["revenue"]),
            chart("bar", "Revenue by customer sector", "sector_type", ["revenue"]),
            chart("bar", "m³ by customer sector", "sector_type", ["consumption"]),
            chart("bar", "Revenue by tariff group", "tariff_desc", ["revenue"], limit=12),
            chart("table", "By billing item", "charge_desc", ["revenue", "consumption", "lines"],
                  share=["revenue"], wide=True),
        ]},
    "sewerage": {
        "title": "Sewerage", "icon": "pipe", "utility": 2,
        "subtitle": "Sewerage billing by sector and tariff",
        "tiles": [tile("revenue", True), tile("consumption", True), tile("customers"), tile("connections"),
                  tile("rev_per_connection")],
        "charts": [
            chart("table", "Sector funnel: customers, consumption and revenue", "sector_desc",
                  ["customers", "consumption", "revenue"], share=["customers", "consumption", "revenue"], wide=True),
            chart("bar", "Revenue by tariff", "tariff_desc", ["revenue"], limit=12),
            chart("bar", "Revenue by invoice origin", "origin_desc", ["revenue"]),
            chart("col", "Revenue by billing period", "period_label", ["revenue"], sort="dim", all_periods=True),
        ]},
    "tariff": {
        "title": "Tariff & Sector", "icon": "tag", "utility": None,
        "subtitle": "What each tariff and sector yields per unit",
        "tiles": [tile("revenue"), tile("rate"), tile("domestic_revenue"), tile("commercial_revenue"),
                  tile("government_revenue")],
        "charts": [
            chart("table", "Tariff groups", "tariff_desc", ["revenue", "consumption", "rate", "customers"],
                  limit=25, wide=True, note="Pick one utility to see consumption and the rate per unit."),
            chart("bar", "Revenue per unit by sector type", "sector_type", ["rate"],
                  note="Pick one utility: kWh and m³ cannot be mixed."),
            chart("table", "Share by sector type", "sector_type", ["revenue", "consumption", "customers"],
                  share=["revenue", "consumption", "customers"]),
        ]},
    "customers": {
        "title": "Customers & Connections", "icon": "users", "utility": None,
        "subtitle": "Who the customers are and how their bills are spread",
        "tiles": [tile("customers", True), tile("connections"), tile("meters"), tile("cons_per_connection"),
                  tile("invoices")],
        "charts": [
            chart("table", "Top customers by revenue", "customer_id", ["cp_revenue", "cp_consumption", "cp_invoices"],
                  series="utility_name", limit=15, account_col=True),
            chart("col", "Revenue by customer rank", "pareto_band", ["cp_revenue"], sort="dim",
                  share=["cp_revenue"]),
            chart("bar", "Customers by bill size", "bill_band", ["cp_customers"], sort="dim"),
            chart("bar", "Connections by consumption band", "consumption_band", ["cn_connections"], sort="dim",
                  one_unit=True, note="Pick one utility: bands are in kWh or m³."),
        ]},
    "controls": {
        "title": "Billing Controls", "icon": "shield", "utility": None,
        "subtitle": "Adjustments, credits, invoice origin and unmetered connections",
        "tiles": [tile("adjustment"), tile("adjustment_rate"), tile("credits"), tile("credit_share"),
                  tile("free_text_invoices"), tile("no_meter_connections")],
        "charts": [
            chart("bar", "Revenue by invoice origin", "origin_desc", ["revenue"]),
            chart("bar", "Invoices by invoice origin", "origin_desc", ["invoices"]),
            chart("bar", "Adjustments by utility", "utility_name", ["adjustment"]),
            chart("table", "Billing lines by item and value type", "charge_desc", ["lines", "revenue"],
                  series="value_desc", limit=30),
        ]},
    "solar": {
        "title": "Solar PV", "icon": "sun", "utility": 1,
        "subtitle": "Customers with photovoltaic installations",
        "tiles": [tile("pv_customers", True), tile("pv_connections"), tile("pv_billing", True), tile("pv_share")],
        "charts": [
            chart("col", "Average kWh per connection: PV vs no PV", "pv_label", ["cn_avg_use"], sort="dim"),
            chart("bar", "PV billing by island", "region_name", ["pv_billing"]),
            chart("col", "PV billing by period", "period_label", ["pv_billing"], sort="dim", all_periods=True),
            chart("table", "PV and non-PV connections", "pv_label", ["cn_connections", "cn_avg_use"]),
        ]},
    "network": {
        "title": "Water Network", "icon": "network", "utility": 3,
        "subtitle": "Water billed by source and network node",
        "tiles": [tile("consumption", True), tile("revenue"), tile("connections"), tile("cons_per_connection")],
        "charts": [
            chart("bar", "m³ by water source", "water_source", ["consumption"], limit=15),
            chart("bar", "m³ by network node", "water_node", ["consumption"], limit=15),
            chart("table", "Sources and nodes", "water_node", ["consumption", "revenue", "connections"],
                  series="water_source", limit=30, wide=True),
        ]},
    "consumption": {
        "title": "Consumption Analytics", "icon": "pulse", "utility": None,
        "subtitle": "Usage bands and connections whose use changed sharply",
        "tiles": [tile("cn_connections"), tile("cn_zero"), tile("cn_jumped"), tile("cn_dropped")],
        "charts": [
            chart("bar", "Connections by consumption band", "consumption_band", ["cn_connections"], sort="dim",
                  one_unit=True, note="Pick one utility: bands are in kWh or m³."),
            chart("bar", "Change since the previous period", "change_flag", ["cn_connections"]),
            chart("table", "By island", "region_name", ["cn_connections", "cn_avg_use", "cn_zero", "cn_jumped",
                                                         "cn_dropped"], wide=True),
        ]},
}


# =============================================================================
# Query building -- every value placed into SQL is checked against a whitelist
# =============================================================================

# Nothing below reads a pre-built table. Every report is parsed and aggregated
# from the raw rows (ub.raw_rows, through the ub.v_lines view) when it is asked
# for, reading only the batches, utilities and islands it needs. Results are
# kept in memory until the raw rows change (a load, or a batch replaced).

import threading
import time
from collections import OrderedDict

_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()
CACHE_SIZE = 3000


def data_version() -> str:
    """Changes whenever raw rows are loaded or replaced."""
    try:
        return ax_load.ch("SELECT count(), max(loaded_at), sum(rows) FROM ub.raw_batches FORMAT TSV").strip()
    except Exception:
        return ""


class Stats:
    """What building one page cost: raw rows read, queries run, how many came from the cache."""
    def __init__(self):
        self.rows = self.queries = self.cached = 0
        self.t0 = time.time()

    def out(self) -> dict:
        return {"raw_rows_read": self.rows, "queries": self.queries, "from_cache": self.cached,
                "ms": round((time.time() - self.t0) * 1000)}


def run(sql: str, version: str | None = None, stats: Stats | None = None) -> list[dict]:
    key = (version, sql)
    if version is not None:
        with _cache_lock:
            if key in _cache:
                _cache.move_to_end(key)
                if stats:
                    stats.queries += 1
                    stats.cached += 1
                return _cache[key]
    r = ax_load.client.post(ax_load.CH, content=(sql + " FORMAT JSON").encode())
    if r.status_code != 200:
        raise RuntimeError(f"ClickHouse: {r.text.strip()}\n--- while running:\n{sql[:500]}")
    data = json.loads(r.text)["data"]
    if stats:
        stats.queries += 1
        try:
            stats.rows += int(json.loads(r.headers.get("X-ClickHouse-Summary", "{}")).get("read_rows", 0))
        except ValueError:
            pass
    if version is not None:
        with _cache_lock:
            _cache[key] = data
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
    return data


def periods(version: str | None = None) -> list[dict]:
    try:
        return run("SELECT toString(period) AS period, period_label, period_index FROM ub.v_periods ORDER BY period",
                   version or data_version())
    except Exception:
        return []  # nothing loaded yet


def period_batches(version: str) -> dict:
    """period -> the batches holding its rows, so a report on one period skips every other batch."""
    rows = run("SELECT toString(period) AS period, groupUniqArray(batch_id) AS batches FROM "
               "(SELECT DISTINCT r.batch_id AS batch_id, if(r.row_month > '1970-01-01', r.row_month, b.batch_month) AS period "
               " FROM ub.raw_rows AS r ANY LEFT JOIN ub.raw_batches AS b ON b.batch_id = r.batch_id) GROUP BY period",
               version)
    return {r["period"]: r["batches"] for r in rows}


SECTORS = ("Domestic", "Commercial", "Government", "Other")

# Chart dimensions a click can turn into a page filter: dim -> (filter key, column holding its value)
FILTER_DIMS = {"region_name": ("region", "region_code"), "utility_name": ("utility", "utility_code"),
               "sector_type": ("sector", "sector_type"), "tariff_desc": ("tariff", "tariff_desc"),
               "period_label": ("period", "period")}


def sql_str(v: str) -> str:
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def tariffs(scope: dict) -> list[dict]:
    """Tariff groups this user can see, biggest first, with the utilities each belongs to."""
    where = f"utility_code IN ({','.join(str(u) for u in scope['utilities']) or 'NULL'})"
    if scope["region"]:
        where += f" AND region_code = {int(scope['region'])}"
    try:
        return run(f"SELECT TARIFFGROUPDESC AS name, groupUniqArray(utility_code) AS utilities FROM ub.raw_rows "
                   f"WHERE {where} AND TARIFFGROUPDESC != '' GROUP BY name ORDER BY sum(toFloat64OrZero(AMOUNT)) DESC",
                   data_version())
    except Exception:
        return []


def scope_where(scope: dict, sel: dict, page_utility, *, all_periods=False) -> str:
    """scope = what the user may see; sel = what they picked (already validated)."""
    utils = set(scope["utilities"])
    if page_utility:
        utils &= {page_utility}
    if sel.get("utility"):
        utils &= {sel["utility"]}
    conds = [f"utility_code IN ({','.join(str(u) for u in sorted(utils)) or 'NULL'})"]
    region = scope["region"] or sel.get("region")
    if region:
        conds.append(f"region_code = {int(region)}")
    if sel.get("period") and not all_periods:
        conds.append(f"period = '{sel['period']}'")
    if sel.get("sector"):
        conds.append(f"sector_type = {sql_str(sel['sector'])}")
    if sel.get("tariff"):
        conds.append(f"tariff_desc = {sql_str(sel['tariff'])}")
    return " AND ".join(conds)


def source(src: str, scope: dict, sel: dict, page_utility, ctx: dict, *, all_periods=False) -> str:
    """The FROM ... WHERE of one report query: the raw rows it needs, parsed, filtered and -- for
    customer (cp) and connection (cn) reports -- rolled up per customer or connection, right now."""
    period = None if all_periods else sel.get("period")
    batches = ctx["batches"]

    def prune(ps):  # only the batches that hold these periods: ClickHouse skips the rest unread
        bs = sorted({b for p in ps for b in batches.get(p, [])})
        return f" AND batch_id IN ({', '.join(sql_str(b) for b in bs) or 'NULL'})"

    if src == "b":
        return f"ub.v_lines WHERE {scope_where(scope, sel, page_utility, all_periods=all_periods)}" + \
            (prune([period]) if period else "")
    if src == "cp":
        lines = f"ub.v_lines WHERE {scope_where(scope, sel, page_utility, all_periods=all_periods)}" + \
            (prune([period]) if period else "")
        return f"""(
    SELECT period, utility_code, customer_id, reg AS region_code, uname AS utility_name, rname AS region_name,
           amt AS amount, cons AS consumption_qty, inv AS invoices,
           multiIf(amt < 0, '1 Credit', amt < 100, '2 Under 100', amt < 500, '3 100-500',
                   amt < 1000, '4 500-1,000', amt < 5000, '5 1,000-5,000',
                   amt < 20000, '6 5,000-20,000', '7 20,000 and over') AS bill_band,
           multiIf(rn <= 0.01 * n, '1 Top 1%', rn <= 0.10 * n, '2 Top 1-10%',
                   rn <= 0.50 * n, '3 Top 10-50%', '4 Bottom 50%') AS pareto_band
    FROM (SELECT *, row_number() OVER (PARTITION BY period, utility_code ORDER BY amt DESC) AS rn,
                 count() OVER (PARTITION BY period, utility_code) AS n
          FROM (SELECT period, utility_code, customer_id, any(region_code) AS reg,
                       any(utility_name) AS uname, any(region_name) AS rname,
                       sum(amount) AS amt, sumIf(quantity, charge_type = 1) AS cons, uniqExact(invoice_id) AS inv
                FROM {lines} GROUP BY period, utility_code, customer_id))) WHERE 1"""
    # cn: a connection's change needs its previous period in the data too, so read that as well
    plist = ctx["periods"]
    idx = {p["period"]: int(p["period_index"]) for p in plist}
    order = "[" + ", ".join(f"toDate('{p['period']}')" for p in plist) + "]"
    if period:
        prev = next((p["period"] for p in plist if int(p["period_index"]) == idx[period] - 1), None)
        want = [period] + ([prev] if prev else [])
        lines = f"ub.v_lines WHERE {scope_where(scope, {**sel, 'period': None}, page_utility)}" + \
            f" AND period IN ({', '.join(sql_str(p) for p in want)})" + prune(want)
    else:
        lines = f"ub.v_lines WHERE {scope_where(scope, sel, page_utility, all_periods=True)}"
    return f"""(
    SELECT period, utility_code, connection_id, reg AS region_code, uname AS utility_name, rname AS region_name,
           pv AS pv_connection, if(pv = 1, 'PV', 'No PV') AS pv_label, amt AS amount, cons AS consumption_qty,
           if(utility_code = 1,
              multiIf(cons <= 0, '0 None', cons <= 100, '1 1-100 kWh', cons <= 200, '2 101-200 kWh',
                      cons <= 300, '3 201-300 kWh', cons <= 500, '4 301-500 kWh', cons <= 1000, '5 501-1,000 kWh',
                      cons <= 5000, '6 1,001-5,000 kWh', '7 Over 5,000 kWh'),
              multiIf(cons <= 0, '0 None', cons <= 5, '1 1-5 m3', cons <= 10, '2 6-10 m3',
                      cons <= 20, '3 11-20 m3', cons <= 50, '4 21-50 m3', cons <= 100, '5 51-100 m3',
                      cons <= 500, '6 101-500 m3', '7 Over 500 m3')) AS consumption_band,
           multiIf(prev_idx IS NULL OR prev_idx != period_index - 1, 'First period',
                   prev_cons > 0 AND cons <= 0, 'Dropped to zero',
                   prev_cons > 0 AND cons >= 3 * prev_cons, 'Jumped 3x or more',
                   prev_cons > 0 AND cons <= prev_cons / 3, 'Fell to a third or less',
                   prev_cons <= 0 AND cons > 0, 'Resumed from zero',
                   cons <= 0, 'Zero in both periods', 'Normal change') AS change_flag
    FROM (SELECT *, lagInFrame(toNullable(period_index)) OVER w AS prev_idx, lagInFrame(cons) OVER w AS prev_cons
          FROM (SELECT period, indexOf({order}, period) AS period_index, utility_code, connection_id,
                       any(region_code) AS reg, any(utility_name) AS uname,
                       any(region_name) AS rname, max(pv_connection) AS pv,
                       sum(amount) AS amt, sumIf(quantity, charge_type = 1) AS cons
                FROM {lines} GROUP BY period, utility_code, connection_id)
          WINDOW w AS (PARTITION BY utility_code, connection_id ORDER BY period_index
                       ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)))
    WHERE {f"period = '{period}'" if period else "1"}"""


def num(v):
    return None if v is None else float(v)


def page_data(page_id: str, scope: dict, sel: dict) -> dict:
    page = PAGES[page_id]
    pu = page["utility"]
    stats = Stats()
    version = data_version()
    plist = periods(version)
    if not plist:
        return {"empty": True}
    ctx = {"periods": plist, "batches": period_batches(version)}
    q = lambda sql: run(sql, version, stats)
    by_period = {p["period"]: p for p in plist}
    prev_period = None
    if sel.get("period") and sel.get("compare") and sel["compare"] != sel["period"]:
        prev_period = None if sel["compare"] == "none" else sel["compare"]
    elif sel.get("period"):
        idx = int(by_period[sel["period"]]["period_index"])
        prev_period = next((p["period"] for p in plist if int(p["period_index"]) == idx - 1), None)

    # --- tiles: one query per source, current and previous period ---------------
    tiles = []
    for src in ("b", "cp", "cn"):
        mine = [t for t in page["tiles"] if METRICS[t["metric"]][1] == src]
        if not mine:
            continue
        cols = ", ".join(f"{METRICS[t['metric']][2]} AS m{i}" for i, t in enumerate(mine))
        cur = q(f"SELECT {cols}, {UNIT[src]} AS unit_label FROM {source(src, scope, sel, pu, ctx)}")[0]
        prev = {}
        if prev_period and any(t["delta"] for t in mine):
            prev = q(f"SELECT {cols} FROM {source(src, scope, {**sel, 'period': prev_period}, pu, ctx)}")[0]
        for i, t in enumerate(mine):
            label, _, _, fmt = METRICS[t["metric"]]
            tiles.append({"metric": t["metric"], "label": label, "fmt": fmt, "unit": cur["unit_label"],
                          "value": num(cur[f"m{i}"]),
                          "previous": num(prev.get(f"m{i}")) if t["delta"] else None,
                          "previous_label": by_period[prev_period]["period_label"] if prev_period and t["delta"] else None})
    order = {t["metric"]: i for i, t in enumerate(page["tiles"])}
    tiles.sort(key=lambda t: order[t["metric"]])

    # --- charts --------------------------------------------------------------------
    charts = []
    for c in page["charts"]:
        src = METRICS[c["metrics"][0]][1]
        dims = [c["dim"]] + ([c["series"]] if c["series"] else [])
        dim_sql = ", ".join(f"toString({d}) AS d{i}" for i, d in enumerate(dims))
        if c["dim"] == "period_label":  # periods need their order and label from pbi_period
            dim_sql = "toString(period) AS d0"
        cols = ", ".join(f"{METRICS[m][2]} AS m{i}" for i, m in enumerate(c["metrics"]))
        fkey = FILTER_DIMS.get(c["dim"])
        if fkey and ((fkey[0] == "utility" and (pu or len(scope["utilities"]) < 2)) or (fkey[0] == "region" and scope["region"])):
            fkey = None  # nothing to narrow: the page or the role already fixes it
        if fkey:
            cols += f", any(toString({fkey[1]})) AS fk"
        # the chart a filter is picked from keeps all its bars (the pick is highlighted), like a cross-filter
        csel = {**sel, fkey[0]: None} if fkey else sel
        frm = source(src, scope, csel, pu, ctx, all_periods=c["all_periods"])
        group = ", ".join(f"d{i}" for i in range(len(dims)))
        if c["sort"] == "dim":
            order_by = group
        else:
            order_by = "m0 DESC NULLS LAST"
        limit = f" LIMIT {int(c['limit'])}" if c["limit"] else ""
        rows = q(f"SELECT {dim_sql}, {cols}, {UNIT[src]} AS unit_label FROM {frm} "
                 f"GROUP BY {group} ORDER BY {order_by}{limit}")
        # the chart's totals (for shares) and its unit, over everything it covers
        tcols = ", ".join(f"{METRICS[m][2]} AS m{i}" for i, m in enumerate(c["metrics"])) if c["share"] else "1 AS m0"
        t = q(f"SELECT {tcols}, {UNIT[src]} AS unit_label FROM {frm}")[0]
        total = {m: num(t[f"m{i}"]) for i, m in enumerate(c["metrics"])} if c["share"] else {}
        unit = t["unit_label"]
        out_rows = []
        for r in rows:
            label = r["d0"]
            if c["dim"] == "period_label":
                label = by_period.get(label, {}).get("period_label", label)
            if c["account_col"] and not scope["see_accounts"]:
                from roles import mask_account
                label = mask_account(label)
            row = {"label": _pretty(c["dim"], label), "values": [num(r[f"m{i}"]) for i in range(len(c["metrics"]))]}
            if c["series"]:
                row["series"] = _pretty(c["series"], r["d1"])
            if fkey and r.get("fk") not in (None, ""):
                row["filter"] = {"key": fkey[0], "value": r["fk"]}
            if c["share"]:
                row["shares"] = [(row["values"][i] / total[m]) if (m in c["share"] and total.get(m)) else None
                                 for i, m in enumerate(c["metrics"])]
            out_rows.append(row)
        needs_unit = c["one_unit"] or any(METRICS[m][3] in ("qty", "rate") for m in c["metrics"])
        charts.append({
            "kind": c["kind"], "title": c["title"], "wide": c["wide"],
            "dim_label": _dim_title(c["dim"]), "series_label": _dim_title(c["series"]) if c["series"] else None,
            "metrics": [{"key": m, "label": METRICS[m][0], "fmt": METRICS[m][3]} for m in c["metrics"]],
            "share": c["share"], "unit": unit, "rows": out_rows,
            "note": c["note"] if needs_unit and not unit else None,
            "all_periods": c["all_periods"], "filter_key": fkey[0] if fkey else None,
        })
    return {"title": page["title"], "subtitle": page["subtitle"], "tiles": tiles, "charts": charts,
            "built": stats.out()}


_DIM_TITLES = {"utility_name": "Utility", "region_name": "Island", "sector_type": "Sector type",
               "sector_desc": "Sector", "tariff_desc": "Tariff group", "category_desc": "Category",
               "charge_desc": "Billing item", "value_desc": "Value type", "origin_desc": "Invoice origin",
               "period_label": "Period", "customer_id": "Customer", "pareto_band": "Customer rank",
               "bill_band": "Bill size", "consumption_band": "Consumption band", "pv_label": "PV",
               "water_source": "Water source", "water_node": "Network node", "change_flag": "Change"}


def _dim_title(d):
    return _DIM_TITLES.get(d, d)


def _pretty(dim, v: str) -> str:
    if v in ("", None):
        return "(not set)"
    if dim in ("pareto_band", "bill_band", "consumption_band") and re.match(r"^\d ", v):
        return v[2:]  # '3 100-500' -> '100-500': the digit only fixes the order
    if dim == "region_name":
        return v.title()
    if dim == "utility_name" and v == "WasteWater":
        return "Sewerage"
    return v
