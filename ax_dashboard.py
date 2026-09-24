#!/usr/bin/env python3
"""Print a ClickHouse /dashboard URL pre-loaded with charts over the ax aggregates.

    python3 ax_dashboard.py              # open the printed URL in a browser

Same trick as dashboard_url.py: the dashboard keeps its whole state in the URL
hash and accepts plain base64 JSON there. Each chart is a query whose first
column `t` is the time axis and every other column is one line.

Change the company, or the date range, in the fields at the top of the page:
{company}, {from} and {to} are query parameters. Amounts are in that company's
own currency, which is why the charts show one company at a time.
"""
import base64
import json
import os

MONTHS = "month >= toDate({from:DateTime}) AND month <= toDate({to:DateTime})"

CHARTS = [
    ("Sales: revenue, cost and margin per month", f"""
SELECT toDateTime(month) AS t, sum(revenue) AS revenue, sum(cost) AS cost, sum(margin) AS margin
FROM ax.agg_sales_monthly
WHERE data_area = {{company:String}} AND {MONTHS}
GROUP BY t ORDER BY t"""),

    ("Sales: order lines per month", f"""
SELECT toDateTime(month) AS t, sum(order_lines) AS order_lines
FROM ax.agg_sales_monthly
WHERE data_area = {{company:String}} AND {MONTHS}
GROUP BY t ORDER BY t"""),

    ("Customer invoices: invoiced (incl. tax) vs net sales", f"""
SELECT toDateTime(month) AS t, sum(invoiced_mst) AS invoiced, sum(net_sales_mst) AS net_sales,
       sum(tax_mst) AS tax
FROM ax.agg_cust_invoice_monthly
WHERE data_area = {{company:String}} AND {MONTHS}
GROUP BY t ORDER BY t"""),

    ("Receivables: invoiced vs paid per month", f"""
SELECT toDateTime(month) AS t, sum(invoiced) AS invoiced, sum(paid) AS paid
FROM ax.agg_ar_monthly
WHERE data_area = {{company:String}} AND {MONTHS}
GROUP BY t ORDER BY t"""),

    ("Purchases: vendor invoice amount per month", f"""
SELECT toDateTime(month) AS t, sum(purchase_amount_mst) AS purchases, sum(tax) AS tax
FROM ax.agg_purchases_monthly
WHERE data_area = {{company:String}} AND {MONTHS}
GROUP BY t ORDER BY t"""),

    ("Payables: billed vs paid per month", f"""
SELECT toDateTime(month) AS t, sum(billed) AS billed, sum(paid_or_credited) AS paid_or_credited
FROM ax.agg_ap_monthly
WHERE data_area = {{company:String}} AND {MONTHS}
GROUP BY t ORDER BY t"""),

    ("Purchase orders created per month", f"""
SELECT toDateTime(month) AS t, sum(purch_orders) AS purch_orders
FROM ax.agg_purch_orders_monthly
WHERE data_area = {{company:String}} AND {MONTHS}
GROUP BY t ORDER BY t"""),

    ("Inventory: quantity in vs out per month", f"""
SELECT toDateTime(month) AS t, sum(qty_in) AS qty_in, sum(qty_out) AS qty_out
FROM ax.agg_inventory_monthly
WHERE data_area = {{company:String}} AND {MONTHS}
GROUP BY t ORDER BY t"""),

    ("Inventory: cost of posted movements per month", f"""
SELECT toDateTime(month) AS t, sum(cost_amount) AS cost_amount
FROM ax.agg_inventory_monthly
WHERE data_area = {{company:String}} AND {MONTHS}
GROUP BY t ORDER BY t"""),

    ("Projects: posted amount per month", f"""
SELECT toDateTime(month) AS t, sum(amount_mst) AS amount, sum(postings) AS postings
FROM ax.agg_project_monthly
WHERE data_area = {{company:String}} AND {MONTHS}
GROUP BY t ORDER BY t"""),
]

state = {
    "host": os.environ.get("CH_DASHBOARD_HOST", "http://localhost:8123/"),
    "user": "default",
    "queries": [{"title": t, "query": q.strip()} for t, q in CHARTS],
    # usmf holds most of its history in 2011-2012; widen the range for other companies.
    "params": {"company": "usmf", "from": "2011-01-01 00:00:00", "to": "2014-12-31 00:00:00",
               "rounding": "86400", "seconds": "86400"},
    "search_query": "",
    "customized": True,
}

if __name__ == "__main__":
    print("http://localhost:8123/dashboard#" + base64.b64encode(json.dumps(state).encode()).decode())
