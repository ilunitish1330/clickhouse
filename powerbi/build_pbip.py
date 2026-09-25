#!/usr/bin/env python3
"""Generate the Power BI project for the PUC utility dashboards.

    python3 powerbi/build_pbip.py      # writes powerbi/PUC Dashboards.pbip + its two folders

Open "PUC Dashboards.pbip" in Power BI Desktop. The model imports from the
ClickHouse `ub` database over plain HTTP (no ODBC driver needed); the server
address, user and password are parameters (Home > Transform data > Edit
parameters), defaulting to http://192.168.11.71:8123/ and user `default`.

ponytail: the project is generated, not hand-edited, so the model, the KPI
measures and the pages stay consistent with each other. Change this file and
re-run it rather than editing the output. Edits made inside Power BI Desktop
and saved are fine too -- they just get overwritten by the next generate.

Format: PBIP with a TMDL semantic model and a PBIR report (report definition
schemas v2.0.0 / 1.0.0 from github.com/microsoft/json-schemas).
"""
import json
import shutil
import uuid
from pathlib import Path

NAME = "PUC Dashboards"
OUT = Path(__file__).resolve().parent
CH_URL, CH_USER = "http://192.168.11.71:8123/", "default"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric"


def guid(*parts: str) -> str:
    """Stable ids, so re-generating does not reshuffle lineage in Power BI."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "puc-dashboards/" + "/".join(parts)))


# =============================================================================
# Semantic model: tables (source, columns) -- all imported from ClickHouse ub
# =============================================================================

D, I, S, N = "dateTime", "int64", "string", "double"
M_TYPE = {D: "type date", I: "Int64.Type", S: "type text", N: "type number"}

TABLES = {
    "Billing": ("ub.pbi_billing", [
        ("period", D), ("utility_code", I), ("region_code", I), ("tariff_code", S), ("charge_key", S),
        ("customer_id", S), ("connection_id", S), ("meter_id", S), ("invoice_id", S), ("invoice_prefix", S),
        ("water_source", S), ("water_node", S), ("pv_connection", I), ("unit", S),
        ("amount", N), ("quantity", N), ("consumption_qty", N), ("consumption_amount", N),
        ("adjustment_amount", N), ("adjustment_qty", N), ("credit_amount", N), ("has_meter", I)]),
    "Period": ("ub.pbi_period", [
        ("period", D), ("year_month", S), ("period_label", S), ("year", I), ("period_index", I)]),
    "Utility": ("ub.dim_utility", [("utility_code", I), ("utility_name", S), ("quantity_unit", S)]),
    "Region": ("ub.dim_region", [("region_code", I), ("region_name", S)]),
    "Tariff": ("ub.pbi_tariff", [
        ("tariff_code", S), ("tariff_desc", S), ("category_desc", S), ("sector_code", S),
        ("sector_desc", S), ("sector_type", S)]),
    "ChargeType": ("ub.pbi_charge_type", [
        ("charge_key", S), ("charge_desc", S), ("value_desc", S), ("origin_desc", S),
        ("is_pv", I), ("is_free_text", I), ("is_consumption", I), ("is_adjustment", I)]),
    "CustomerPeriod": ("ub.pbi_customer_period", [
        ("period", D), ("utility_code", I), ("customer_id", S), ("amount", N), ("consumption_qty", N),
        ("invoices", I), ("bill_band", S), ("pareto_band", S), ("utility_mix", S)]),
    "ConnectionPeriod": ("ub.pbi_connection_period", [
        ("period", D), ("utility_code", I), ("connection_id", S), ("region_code", I), ("pv_connection", I),
        ("pv_label", S), ("amount", N), ("consumption_qty", N), ("consumption_band", S), ("change_flag", S)]),
}
HIDDEN = {("Billing", c) for c in ("utility_code", "region_code", "tariff_code", "charge_key", "period")} | \
         {(t, c) for t in ("CustomerPeriod", "ConnectionPeriod") for c in ("utility_code", "region_code", "period")} | \
         {("Period", "period_index")}
SORT_BY = {("Period", "period_label"): "period_index", ("Period", "year_month"): "period_index"}

RELATIONSHIPS = [
    ("Billing", "period", "Period", "period"), ("Billing", "utility_code", "Utility", "utility_code"),
    ("Billing", "region_code", "Region", "region_code"), ("Billing", "tariff_code", "Tariff", "tariff_code"),
    ("Billing", "charge_key", "ChargeType", "charge_key"),
    ("CustomerPeriod", "period", "Period", "period"), ("CustomerPeriod", "utility_code", "Utility", "utility_code"),
    ("ConnectionPeriod", "period", "Period", "period"),
    ("ConnectionPeriod", "utility_code", "Utility", "utility_code"),
    ("ConnectionPeriod", "region_code", "Region", "region_code"),
]

# =============================================================================
# KPI measures -- the catalogue's formulas (PUC_Water_Dashboard_KPI_Catalogue)
# name: (table, DAX, format, folder)
# =============================================================================

MONEY, NUM, RATE, PCT, COUNT = "#,0", "#,0", "#,0.00", "0.0%", "#,0"
# kWh and m3 cannot be added: a quantity measure is blank unless one unit is in view
ONE_UNIT = "IF(DISTINCTCOUNT(Billing[unit]) = 1, {})"


def prev(measure: str) -> str:
    return (f"VAR i = MAX(Period[period_index])\n"
            f"RETURN CALCULATE([{measure}], REMOVEFILTERS(Period), Period[period_index] = i - 1)")


def growth(measure: str, previous: str) -> str:
    """Latest selected period vs the one before it in the data -- with no period
    picked, 'current' is the latest period, not all periods added up."""
    return (f"VAR i = MAX(Period[period_index])\n"
            f"VAR cur = CALCULATE([{measure}], Period[period_index] = i)\n"
            f"VAR p = [{previous}]\n"
            f"RETURN IF(NOT ISBLANK(p) && p <> 0, DIVIDE(cur - p, ABS(p)))")


MEASURES = {
    # revenue
    "Total Revenue": ("Billing", "SUM(Billing[amount])", MONEY, "Revenue"),
    "Revenue Previous Period": ("Billing", prev("Total Revenue"), MONEY, "Revenue"),
    "Revenue Growth %": ("Billing", growth("Total Revenue", "Revenue Previous Period"), PCT, "Revenue"),
    "Average Revenue per Customer": ("Billing", "DIVIDE([Total Revenue], [Distinct Customers])", RATE, "Revenue"),
    "Average Revenue per Connection": ("Billing", "DIVIDE([Total Revenue], [Distinct Connections])", RATE, "Revenue"),
    "Average Revenue per Invoice": ("Billing", "DIVIDE([Total Revenue], [Invoice Count])", RATE, "Revenue"),
    "Revenue Share %": ("Billing", "DIVIDE([Total Revenue], CALCULATE([Total Revenue], ALLSELECTED()))", PCT, "Revenue"),
    "Domestic Revenue": ("Billing", "CALCULATE([Total Revenue], Tariff[sector_type] = \"Domestic\")", MONEY, "Revenue"),
    "Commercial Revenue": ("Billing", "CALCULATE([Total Revenue], Tariff[sector_type] = \"Commercial\")", MONEY, "Revenue"),
    "Government Revenue": ("Billing", "CALCULATE([Total Revenue], Tariff[sector_type] = \"Government\")", MONEY, "Revenue"),
    # consumption
    "Total Consumption": ("Billing", ONE_UNIT.format("SUM(Billing[quantity])"), NUM, "Consumption"),
    "Consumption Item Quantity": ("Billing", ONE_UNIT.format("SUM(Billing[consumption_qty])"), NUM, "Consumption"),
    "Consumption Item Revenue": ("Billing", "SUM(Billing[consumption_amount])", MONEY, "Consumption"),
    "Average Consumption Bill": ("Billing", "DIVIDE([Consumption Item Revenue], [Consumption Item Quantity])", RATE, "Consumption"),
    "Revenue per Unit": ("Billing", "DIVIDE([Total Revenue], [Total Consumption])", RATE, "Consumption"),
    "Average Consumption per Customer": ("Billing", "DIVIDE([Total Consumption], [Distinct Customers])", RATE, "Consumption"),
    "Average Consumption per Connection": ("Billing", "DIVIDE([Total Consumption], [Distinct Connections])", RATE, "Consumption"),
    "Average Consumption per Invoice": ("Billing", "DIVIDE([Total Consumption], [Invoice Count])", RATE, "Consumption"),
    "Consumption Previous Period": ("Billing", prev("Total Consumption"), NUM, "Consumption"),
    "Consumption Growth %": ("Billing", growth("Total Consumption", "Consumption Previous Period"), PCT, "Consumption"),
    "Consumption Share %": ("Billing", "DIVIDE([Total Consumption], CALCULATE([Total Consumption], ALLSELECTED()))", PCT, "Consumption"),
    "Domestic Consumption": ("Billing", "CALCULATE([Total Consumption], Tariff[sector_type] = \"Domestic\")", NUM, "Consumption"),
    "Commercial Consumption": ("Billing", "CALCULATE([Total Consumption], Tariff[sector_type] = \"Commercial\")", NUM, "Consumption"),
    # customers
    "Distinct Customers": ("Billing", "DISTINCTCOUNT(Billing[customer_id])", COUNT, "Customers"),
    "Distinct Connections": ("Billing", "DISTINCTCOUNT(Billing[connection_id])", COUNT, "Customers"),
    "Distinct Meters": ("Billing", "CALCULATE(DISTINCTCOUNT(Billing[meter_id]), Billing[has_meter] = 1)", COUNT, "Customers"),
    "Invoice Count": ("Billing", "DISTINCTCOUNT(Billing[invoice_id])", COUNT, "Customers"),
    "Billing Lines": ("Billing", "COUNTROWS(Billing)", COUNT, "Customers"),
    "Customers Previous Period": ("Billing", prev("Distinct Customers"), COUNT, "Customers"),
    "Customer Growth %": ("Billing", growth("Distinct Customers", "Customers Previous Period"), PCT, "Customers"),
    "Customer Share %": ("Billing", "DIVIDE([Distinct Customers], CALCULATE([Distinct Customers], ALLSELECTED()))", PCT, "Customers"),
    # billing controls
    "Adjustment Amount": ("Billing", "SUM(Billing[adjustment_amount])", MONEY, "Controls"),
    "Adjusted Quantity": ("Billing", ONE_UNIT.format("SUM(Billing[adjustment_qty])"), NUM, "Controls"),
    "Adjustment Rate %": ("Billing", "DIVIDE(ABS([Adjustment Amount]), ABS([Total Revenue]))", PCT, "Controls"),
    "Credit Amount": ("Billing", "SUM(Billing[credit_amount])", MONEY, "Controls"),
    "Credit Share %": ("Billing", "DIVIDE(ABS([Credit Amount]), ABS([Total Revenue]))", PCT, "Controls"),
    "Invoice Share %": ("Billing", "DIVIDE([Invoice Count], CALCULATE([Invoice Count], ALLSELECTED()))", PCT, "Controls"),
    "Free-text Invoices": ("Billing", "CALCULATE([Invoice Count], ChargeType[is_free_text] = 1)", COUNT, "Controls"),
    "Connections without Meter": ("Billing", "CALCULATE([Distinct Connections], Billing[has_meter] = 0)", COUNT, "Controls"),
    # solar
    "PV Customers": ("Billing", "CALCULATE([Distinct Customers], Billing[pv_connection] = 1)", COUNT, "Solar PV"),
    "PV Connections": ("Billing", "CALCULATE([Distinct Connections], Billing[pv_connection] = 1)", COUNT, "Solar PV"),
    "PV Billing": ("Billing", "CALCULATE([Total Revenue], ChargeType[is_pv] = 1)", MONEY, "Solar PV"),
    "PV Billing Share %": ("Billing", "DIVIDE([PV Billing], [Total Revenue])", PCT, "Solar PV"),
    # per customer / per connection analytics
    "Customers in Band": ("CustomerPeriod", "DISTINCTCOUNT(CustomerPeriod[customer_id])", COUNT, "Analytics"),
    "Customer Revenue": ("CustomerPeriod", "SUM(CustomerPeriod[amount])", MONEY, "Analytics"),
    "Customer Revenue Share %": ("CustomerPeriod", "DIVIDE([Customer Revenue], CALCULATE([Customer Revenue], ALLSELECTED()))", PCT, "Analytics"),
    "Multi-utility Customers": ("CustomerPeriod", "CALCULATE(DISTINCTCOUNT(CustomerPeriod[customer_id]), CustomerPeriod[utility_mix] = \"Electricity and water\")", COUNT, "Analytics"),
    "Connections Billed": ("ConnectionPeriod", "COUNTROWS(ConnectionPeriod)", COUNT, "Analytics"),
    "Average Use per Connection": ("ConnectionPeriod", "IF(DISTINCTCOUNT(ConnectionPeriod[utility_code]) = 1, AVERAGE(ConnectionPeriod[consumption_qty]))", RATE, "Analytics"),
    "Zero-consumption Connections": ("ConnectionPeriod", "CALCULATE(COUNTROWS(ConnectionPeriod), ConnectionPeriod[consumption_qty] <= 0)", COUNT, "Analytics"),
    "Connections Jumped 3x": ("ConnectionPeriod", "CALCULATE(COUNTROWS(ConnectionPeriod), ConnectionPeriod[change_flag] = \"Jumped 3x or more\")", COUNT, "Analytics"),
    "Connections Dropped to Zero": ("ConnectionPeriod", "CALCULATE(COUNTROWS(ConnectionPeriod), ConnectionPeriod[change_flag] = \"Dropped to zero\")", COUNT, "Analytics"),
}


def tmdl_name(n: str) -> str:
    return n if n.replace("_", "").isalnum() else "'" + n.replace("'", "''") + "'"


def m_query(table: str) -> str:
    src, cols = TABLES[table]
    sql = f"SELECT {', '.join(c for c, _ in cols)} FROM {src} FORMAT CSVWithNames"
    types = ", ".join(f'{{"{c}", {M_TYPE[t]}}}' for c, t in cols)
    return "\n".join([
        "let",
        "    Source = Csv.Document(",
        "        Web.Contents(ClickHouseUrl, [",
        f'            Query = [query = "{sql}", user = ClickHouseUser, password = ClickHousePassword],',
        "            Timeout = #duration(0, 0, 30, 0)]),",
        "        [Delimiter = \",\", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),",
        "    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),",
        f"    Typed = Table.TransformColumnTypes(Promoted, {{{types}}}, \"en-US\")",
        "in",
        "    Typed"])


def table_tmdl(table: str) -> str:
    _, cols = TABLES[table]
    L = [f"table {tmdl_name(table)}", f"\tlineageTag: {guid('t', table)}", ""]
    for name, (t, dax, fmt, folder) in MEASURES.items():
        if t != table:
            continue
        lines = dax.split("\n")
        if len(lines) == 1:
            L.append(f"\tmeasure {tmdl_name(name)} = {dax}")
        else:
            L.append(f"\tmeasure {tmdl_name(name)} =")
            L += [f"\t\t\t{l}" for l in lines]
        L += [f"\t\tformatString: {fmt}", f"\t\tdisplayFolder: {folder}",
              f"\t\tlineageTag: {guid('m', table, name)}", ""]
    for c, typ in cols:
        L.append(f"\tcolumn {tmdl_name(c)}")
        L.append(f"\t\tdataType: {typ}")
        if typ == D:
            L.append("\t\tformatString: yyyy-mm-dd")
        elif typ == N:
            L.append("\t\tformatString: #,0.00")
        if (table, c) in HIDDEN:
            L.append("\t\tisHidden")
        L.append(f"\t\tlineageTag: {guid('c', table, c)}")
        L.append(f"\t\tsummarizeBy: {'sum' if typ == N else 'none'}")
        L.append(f"\t\tsourceColumn: {c}")
        if (table, c) in SORT_BY:
            L.append(f"\t\tsortByColumn: {SORT_BY[(table, c)]}")
        L += ["", "\t\tannotation SummarizationSetBy = Automatic", ""]
    L.append(f"\tpartition {tmdl_name(table)} = m")
    L.append("\t\tmode: import")
    L.append("\t\tsource =")
    L += [f"\t\t\t\t{l}" for l in m_query(table).split("\n")]
    L += ["", "\tannotation PBI_ResultType = Table", ""]
    return "\n".join(L)


def semantic_model(root: Path) -> None:
    d = root / "definition"
    (d / "tables").mkdir(parents=True)
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1567\n\n")
    order = ["ClickHouseUrl", "ClickHouseUser", "ClickHousePassword"] + list(TABLES)
    model = ["model Model", "\tculture: en-US", "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
             "\tsourceQueryCulture: en-US", "\tdataAccessOptions", "\t\tlegacyRedirects",
             "\t\treturnErrorValuesAsNull", "",
             f"annotation PBI_QueryOrder = {json.dumps(order)}", "",
             "annotation __PBI_TimeIntelligenceEnabled = 0", ""]
    model += [f"ref table {tmdl_name(t)}" for t in TABLES]
    (d / "model.tmdl").write_text("\n".join(model) + "\n\n")
    exprs = []
    for name, value in (("ClickHouseUrl", CH_URL), ("ClickHouseUser", CH_USER), ("ClickHousePassword", "")):
        exprs += [f'expression {name} = "{value}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=false]',
                  f"\tlineageTag: {guid('e', name)}", "", "\tannotation PBI_ResultType = Text", ""]
    (d / "expressions.tmdl").write_text("\n".join(exprs) + "\n")
    rels = []
    for ft, fc, tt, tc in RELATIONSHIPS:
        rels += [f"relationship {guid('r', ft, fc, tt)}", f"\tfromColumn: {tmdl_name(ft)}.{tmdl_name(fc)}",
                 f"\ttoColumn: {tmdl_name(tt)}.{tmdl_name(tc)}"]
        if fc == "period":
            rels.append("\tjoinOnDateBehavior: datePartOnly")
        rels.append("")
    (d / "relationships.tmdl").write_text("\n".join(rels) + "\n")
    for t in TABLES:
        (d / "tables" / f"{t}.tmdl").write_text(table_tmdl(t))
    (root / "definition.pbism").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.0", "settings": {}}, indent=2))
    platform(root, "SemanticModel")


# =============================================================================
# Report: pages and visuals
# =============================================================================

def measure(name: str) -> dict:
    t = MEASURES[name][0]
    return {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": t}}, "Property": name}},
            "queryRef": f"{t}.{name}", "nativeQueryRef": name}


def column(table: str, col: str) -> dict:
    return {"field": {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": col}},
            "queryRef": f"{table}.{col}", "nativeQueryRef": col}


def lit(v) -> dict:
    return {"expr": {"Literal": {"Value": v}}}


def text(s: str) -> dict:
    return lit("'" + s.replace("'", "''") + "'")


def visual(kind: str, box, roles: dict, title: str | None = None, sort=None, objects=None) -> dict:
    x, y, w, h = box
    v = {"visualType": kind,
         "query": {"queryState": {role: {"projections": fields} for role, fields in roles.items()}},
         "drillFilterOtherVisuals": True}
    if sort:
        field, direction = sort
        v["query"]["sortDefinition"] = {"sort": [{"field": field["field"], "direction": direction}],
                                        "isDefaultSort": True}
    if objects:
        v["objects"] = objects
    if title:
        v["visualContainerObjects"] = {"title": [{"properties": {"show": lit("true"), "text": text(title)}}]}
    return {"position": {"x": x, "y": y, "z": 0, "width": w, "height": h}, "visual": v}


# layout: 1280 x 720; slicers on top, a row of cards, then charts
W, H, G = 1280, 720, 12


def slicers(*fields, single=None):
    out, x = [], G
    for f in fields:
        objs = {"data": [{"properties": {"mode": text("Dropdown")}}]}
        if single and f is single:
            objs["selection"] = [{"properties": {"singleSelect": lit("true")}}]
        out.append(visual("slicer", (x, G, 220, 60), {"Values": [f]}, objects=objs))
        x += 220 + G
    return out


def cards(*names):
    n = len(names)
    w = (W - G * (n + 1)) / n
    return [visual("card", (G + i * (w + G), 84, w, 92), {"Values": [measure(m)]}) for i, m in enumerate(names)]


def grid(n_cols: int, row: int):
    """Box for a chart: two rows under the cards, n_cols across."""
    top, height = 188, (H - 188 - 2 * G) / 2
    w = (W - G * (n_cols + 1)) / n_cols

    def box(col, span=1):
        return (G + col * (w + G), top + row * (height + G), w * span + G * (span - 1), height)
    return box


def bar(kind, box, cat, *ys, title, series=None, sort_desc=True):
    roles = {"Category": [cat], "Y": [measure(y) for y in ys]}
    if series:
        roles["Series"] = [series]
    return visual(kind, box, roles, title, sort=(measure(ys[0]), "Descending") if sort_desc else (cat, "Ascending"))


def table(box, fields, title, sort=None):
    return visual("tableEx", box, {"Values": fields}, title, sort=sort)


P, U, R, T, CT = "Period", "Utility", "Region", "Tariff", "ChargeType"
period, region, util = column(P, "period_label"), column(R, "region_name"), column(U, "utility_name")


def utility_filter(name: str) -> dict:
    return {"filters": [{
        "name": "utility_" + name.lower(), "type": "Categorical",
        "field": {"Column": {"Expression": {"SourceRef": {"Entity": U}}, "Property": "utility_name"}},
        "filter": {"Version": 2, "From": [{"Name": "u", "Entity": U, "Type": 0}],
                   "Where": [{"Condition": {"In": {
                       "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "u"}}, "Property": "utility_name"}}],
                       "Values": [[{"Literal": {"Value": f"'{name}'"}}]]}}}]},
        "howCreated": "User"}]}


def pages() -> list[tuple[str, str, dict | None, list]]:
    g2, g3 = grid(2, 0), grid(3, 0)
    g2b, g3b = grid(2, 1), grid(3, 1)
    util_single = column(U, "utility_name")
    out = []

    out.append(("p01_executive", "1 Executive Overview", None, [
        *slicers(period, region),
        *cards("Total Revenue", "Revenue Growth %", "Distinct Customers", "Distinct Connections",
               "Invoice Count", "Average Revenue per Customer"),
        bar("clusteredColumnChart", g3(0), util, "Total Revenue", title="Revenue by utility"),
        bar("clusteredBarChart", g3(1), region, "Total Revenue", title="Revenue by island and utility", series=util),
        visual("donutChart", g3(2), {"Category": [column(T, "sector_type")], "Y": [measure("Total Revenue")]},
               "Revenue by sector type"),
        table(g2b(0, 2), [util, column(U, "quantity_unit"), measure("Total Revenue"), measure("Total Consumption"),
                          measure("Revenue per Unit"), measure("Distinct Customers"), measure("Revenue Growth %"),
                          measure("Consumption Growth %")], "Utilities at a glance"),
    ]))

    def utility_page(pid, title, name, card_names, extra):
        return (pid, title, utility_filter(name), [*slicers(period, region), *cards(*card_names), *extra])

    out.append(utility_page("p02_electricity", "2 Electricity", "Electricity",
        ["Total Revenue", "Total Consumption", "Revenue per Unit", "Distinct Customers",
         "Average Revenue per Customer", "Average Consumption per Customer"], [
        bar("clusteredBarChart", g2(0), region, "Total Revenue", title="Revenue by region"),
        bar("clusteredBarChart", g2(1), column(T, "sector_desc"), "Total Consumption", title="kWh by sector"),
        bar("clusteredBarChart", g2b(0), column(T, "tariff_desc"), "Total Revenue", title="Revenue by tariff group"),
        table(g2b(1), [column(T, "category_desc"), measure("Total Revenue"), measure("Total Consumption"),
                       measure("Revenue per Unit"), measure("Invoice Count"), measure("Average Revenue per Invoice")],
              "By category", sort=(measure("Total Revenue"), "Descending")),
    ]))
    out.append(utility_page("p03_water", "3 Water", "Water",
        ["Total Revenue", "Total Consumption", "Revenue per Unit", "Distinct Customers",
         "Consumption Item Revenue", "Average Consumption Bill"], [
        bar("clusteredBarChart", g2(0), region, "Total Revenue", title="Revenue by region"),
        bar("clusteredColumnChart", g2(1), column(T, "sector_type"), "Total Revenue", "Total Consumption",
            title="Domestic vs commercial: revenue and m3"),
        bar("clusteredBarChart", g2b(0), column(T, "tariff_desc"), "Total Revenue", title="Revenue by tariff group"),
        table(g2b(1), [column(CT, "charge_desc"), measure("Total Revenue"), measure("Total Consumption"),
                       measure("Revenue Share %")], "By billing item", sort=(measure("Total Revenue"), "Descending")),
    ]))
    out.append(utility_page("p04_sewerage", "4 Sewerage", "WasteWater",
        ["Total Revenue", "Total Consumption", "Distinct Customers", "Distinct Connections",
         "Average Revenue per Connection", "Revenue Growth %"], [
        table(g2(0), [column(T, "sector_desc"), measure("Distinct Customers"), measure("Customer Share %"),
                      measure("Total Consumption"), measure("Consumption Share %"), measure("Total Revenue"),
                      measure("Revenue Share %")], "Sector funnel: customers, consumption, revenue",
              sort=(measure("Total Revenue"), "Descending")),
        bar("clusteredBarChart", g2(1), column(T, "tariff_desc"), "Total Revenue", title="Revenue by tariff"),
        bar("clusteredColumnChart", g2b(0), period, "Total Revenue", title="Revenue by period", sort_desc=False),
        visual("donutChart", g2b(1), {"Category": [column(CT, "origin_desc")], "Y": [measure("Total Revenue")]},
               "Revenue by invoice origin"),
    ]))
    out.append(("p05_tariff", "5 Tariff and Sector", None, [
        *slicers(period, region, util_single, single=util_single),
        *cards("Total Revenue", "Revenue per Unit", "Domestic Revenue", "Commercial Revenue", "Government Revenue"),
        table(g2(0, 2), [column(T, "tariff_desc"), column(T, "sector_desc"), measure("Total Revenue"),
                         measure("Total Consumption"), measure("Revenue per Unit"), measure("Distinct Customers")],
              "Tariff groups: pick ONE utility above for consumption and rate",
              sort=(measure("Total Revenue"), "Descending")),
        bar("clusteredColumnChart", g2b(0), column(T, "sector_type"), "Revenue per Unit",
            title="Rate per unit by sector type (cross-subsidy)"),
        table(g2b(1), [column(T, "sector_type"), measure("Revenue Share %"), measure("Consumption Share %"),
                       measure("Customer Share %")], "Share by sector type"),
    ]))
    out.append(("p06_customers", "6 Customers and Connections", None, [
        *slicers(period, util_single, single=util_single),
        *cards("Distinct Customers", "Distinct Connections", "Distinct Meters", "Average Consumption per Connection",
               "Customer Growth %", "Invoice Count"),  # accounts are per utility: none hold both
        table(g3(0), [column("CustomerPeriod", "customer_id"), measure("Customer Revenue")], "Top customers",
              sort=(measure("Customer Revenue"), "Descending")),
        bar("clusteredColumnChart", g3(1), column("CustomerPeriod", "pareto_band"), "Customer Revenue Share %",
            title="Share of revenue by customer rank", sort_desc=False),
        bar("clusteredColumnChart", g3(2), column("CustomerPeriod", "bill_band"), "Customers in Band",
            title="Customers by bill size", sort_desc=False),
        bar("clusteredColumnChart", g2b(0, 2), column("ConnectionPeriod", "consumption_band"), "Connections Billed",
            title="Connections by consumption band", sort_desc=False),
    ]))
    out.append(("p07_controls", "7 Billing Controls", None, [
        *slicers(period, util, region),
        *cards("Adjustment Amount", "Adjustment Rate %", "Credit Amount", "Credit Share %",
               "Free-text Invoices", "Connections without Meter"),
        bar("clusteredBarChart", g2(0), column(CT, "origin_desc"), "Total Revenue", title="Revenue by invoice origin"),
        bar("clusteredColumnChart", g2(1), column(CT, "origin_desc"), "Invoice Share %", title="Invoice origin mix"),
        table(g2b(0), [column(CT, "charge_desc"), column(CT, "value_desc"), measure("Billing Lines"),
                       measure("Total Revenue")], "Billing lines by item and value type",
              sort=(measure("Total Revenue"), "Descending")),
        bar("clusteredBarChart", g2b(1), util, "Adjustment Amount", title="Adjustments by utility"),
    ]))
    out.append(("p08_solar", "8 Solar PV", utility_filter("Electricity"), [
        *slicers(period, region),
        *cards("PV Customers", "PV Connections", "PV Billing", "PV Billing Share %"),
        bar("clusteredColumnChart", g2(0), column("ConnectionPeriod", "pv_label"), "Average Use per Connection",
            title="Average kWh per connection: PV vs no PV", sort_desc=False),
        bar("clusteredBarChart", g2(1), region, "PV Billing", title="PV billing by region"),
        bar("clusteredColumnChart", g2b(0), period, "PV Billing", title="PV billing by period", sort_desc=False),
        table(g2b(1), [column("ConnectionPeriod", "pv_label"), measure("Connections Billed"),
                       measure("Average Use per Connection")], "PV and non-PV connections"),
    ]))
    out.append(("p09_network", "9 Water Network", utility_filter("Water"), [
        *slicers(period, region),
        *cards("Total Consumption", "Total Revenue", "Distinct Connections", "Average Consumption per Connection"),
        bar("clusteredBarChart", g2(0), column("Billing", "water_source"), "Total Consumption",
            title="m3 by water source"),
        bar("clusteredBarChart", g2(1), column("Billing", "water_node"), "Total Consumption",
            title="m3 by network node"),
        table(g2b(0, 2), [column("Billing", "water_source"), column("Billing", "water_node"),
                          measure("Total Consumption"), measure("Total Revenue"), measure("Distinct Connections"),
                          measure("Average Consumption per Connection")], "Sources and nodes",
              sort=(measure("Total Consumption"), "Descending")),
    ]))
    out.append(("p10_consumption", "10 Consumption Analytics", None, [
        *slicers(period, util_single, region, single=util_single),
        *cards("Connections Billed", "Zero-consumption Connections", "Connections Jumped 3x",
               "Connections Dropped to Zero"),
        bar("clusteredColumnChart", g2(0), column("ConnectionPeriod", "consumption_band"), "Connections Billed",
            title="Connections by consumption band", sort_desc=False),
        bar("clusteredBarChart", g2(1), column("ConnectionPeriod", "change_flag"), "Connections Billed",
            title="Change since the previous period"),
        table(g2b(0, 2), [region, measure("Connections Billed"), measure("Average Use per Connection"),
                          measure("Zero-consumption Connections"), measure("Connections Jumped 3x"),
                          measure("Connections Dropped to Zero")], "By region"),
    ]))
    return out


def report(root: Path) -> None:
    d = root / "definition"
    (d / "pages").mkdir(parents=True)
    (d / "version.json").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definition/versionMetadata/1.0.0/schema.json", "version": "2.0.0"}, indent=2))
    (d / "report.json").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definition/report/2.0.0/schema.json", "themeCollection": {}}, indent=2))
    all_pages = pages()
    (d / "pages" / "pages.json").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definition/pagesMetadata/1.0.0/schema.json",
        "pageOrder": [p[0] for p in all_pages], "activePageName": all_pages[0][0]}, indent=2))
    for pid, title, filters, visuals in all_pages:
        pd = d / "pages" / pid
        (pd / "visuals").mkdir(parents=True)
        page = {"$schema": f"{SCHEMA}/item/report/definition/page/2.0.0/schema.json",
                "name": pid, "displayName": title, "displayOption": "FitToPage", "height": H, "width": W}
        if filters:
            page["filterConfig"] = filters
        (pd / "page.json").write_text(json.dumps(page, indent=2))
        for i, v in enumerate(visuals):
            vid = f"{pid}_v{i:02d}"
            v["position"]["z"] = i * 1000
            v["position"]["tabOrder"] = i * 1000
            (pd / "visuals" / vid).mkdir()
            (pd / "visuals" / vid / "visual.json").write_text(json.dumps({
                "$schema": f"{SCHEMA}/item/report/definition/visualContainer/2.0.0/schema.json",
                "name": vid, **v}, indent=2))
    (root / "definition.pbir").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}}, indent=2))
    platform(root, "Report")


def platform(root: Path, kind: str) -> None:
    (root / ".platform").write_text(json.dumps({
        "$schema": f"{SCHEMA}/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": kind, "displayName": NAME},
        "config": {"version": "2.0", "logicalId": guid("item", kind)}}, indent=2))


def main() -> None:
    for p in (OUT / f"{NAME}.SemanticModel", OUT / f"{NAME}.Report"):
        shutil.rmtree(p, ignore_errors=True)
    semantic_model(OUT / f"{NAME}.SemanticModel")
    report(OUT / f"{NAME}.Report")
    (OUT / f"{NAME}.pbip").write_text(json.dumps({
        "$schema": f"{SCHEMA}/pbip/pbipProperties/1.0.0/schema.json", "version": "1.0",
        "artifacts": [{"report": {"path": f"{NAME}.Report"}}], "settings": {"enableAutoRecovery": True}}, indent=2))
    n_vis = sum(len(v) for *_, v in pages())
    print(f"wrote {OUT / NAME}.pbip: {len(TABLES)} tables, {len(RELATIONSHIPS)} relationships, "
          f"{len(MEASURES)} measures, {len(pages())} pages, {n_vis} visuals")


if __name__ == "__main__":
    main()
