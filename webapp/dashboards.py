"""The ten dashboards: every tile and chart as a metric over a ClickHouse table.

Metrics mirror the KPI catalogue and the Power BI measures (powerbi/build_pbip.py).
A page query always runs through scope_where(), which applies the user's role
and island BEFORE the user's own filters -- a filter can narrow what a user sees,
never widen it.
"""
import json
import re

import ax_load  # ch()

SRC = {"b": "ub.app_billing", "cp": "ub.app_customer_period", "cn": "ub.app_connection_period"}
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
    "multi_utility": ("Customers with electricity and water", "cp",
                      "uniqExactIf(customer_id, utility_mix = 'Electricity and water')", "count"),
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

def periods() -> list[dict]:
    try:
        rows = json.loads(ax_load.ch("SELECT toString(period) AS period, period_label, period_index "
                                     "FROM ub.pbi_period ORDER BY period FORMAT JSON"))["data"]
    except Exception:
        return []  # nothing loaded yet
    return rows


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
    return " AND ".join(conds)


def run(sql: str) -> list[dict]:
    return json.loads(ax_load.ch(sql + " FORMAT JSON"))["data"]


def num(v):
    return None if v is None else float(v)


def page_data(page_id: str, scope: dict, sel: dict) -> dict:
    page = PAGES[page_id]
    pu = page["utility"]
    plist = periods()
    if not plist:
        return {"empty": True}
    by_period = {p["period"]: p for p in plist}
    prev_period = None
    if sel.get("period"):
        idx = int(by_period[sel["period"]]["period_index"])
        prev_period = next((p["period"] for p in plist if int(p["period_index"]) == idx - 1), None)

    # --- tiles: one query per source, current and previous period ---------------
    tiles = []
    for src in ("b", "cp", "cn"):
        mine = [t for t in page["tiles"] if METRICS[t["metric"]][1] == src]
        if not mine:
            continue
        cols = ", ".join(f"{METRICS[t['metric']][2]} AS m{i}" for i, t in enumerate(mine))
        cur = run(f"SELECT {cols}, {UNIT[src]} AS unit_label FROM {SRC[src]} WHERE {scope_where(scope, sel, pu)}")[0]
        prev = {}
        if prev_period and any(t["delta"] for t in mine):
            prev = run(f"SELECT {cols} FROM {SRC[src]} "
                       f"WHERE {scope_where(scope, {**sel, 'period': prev_period}, pu)}")[0]
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
        where = scope_where(scope, sel, pu, all_periods=c["all_periods"])
        group = ", ".join(f"d{i}" for i in range(len(dims)))
        if c["sort"] == "dim":
            order_by = group
        else:
            order_by = "m0 DESC NULLS LAST"
        limit = f" LIMIT {int(c['limit'])}" if c["limit"] else ""
        rows = run(f"SELECT {dim_sql}, {cols}, {UNIT[src]} AS unit_label FROM {SRC[src]} WHERE {where} "
                   f"GROUP BY {group} ORDER BY {order_by}{limit}")
        total = {}
        if c["share"]:
            t = run(f"SELECT {', '.join(f'{METRICS[m][2]} AS m{i}' for i, m in enumerate(c['metrics']))} "
                    f"FROM {SRC[src]} WHERE {where}")[0]
            total = {m: num(t[f"m{i}"]) for i, m in enumerate(c["metrics"])}
        unit = run(f"SELECT {UNIT[src]} AS unit_label FROM {SRC[src]} WHERE {where}")[0]["unit_label"]
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
            "all_periods": c["all_periods"],
        })
    return {"title": page["title"], "subtitle": page["subtitle"], "tiles": tiles, "charts": charts}


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
