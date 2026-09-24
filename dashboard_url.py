#!/usr/bin/env python3
"""Print a /dashboard URL pre-loaded with charts over learn.events.

The built-in dashboard keeps its whole state in the URL hash. It normally
LZ-compresses it, but decodeState() falls back to atob() -- so plain base64
JSON is a valid hash and we can build one without the LZString dependency.
"""
import base64, json

CHARTS = [
    ("Events per interval", """
SELECT toStartOfInterval(ts, INTERVAL {rounding:UInt32} SECOND) AS t, count() AS events
FROM learn.events WHERE ts >= {from:DateTime} AND ts <= {to:DateTime}
GROUP BY t ORDER BY t"""),

    ("Revenue per interval", """
SELECT toStartOfInterval(ts, INTERVAL {rounding:UInt32} SECOND) AS t, sum(revenue) AS revenue
FROM learn.events WHERE ts >= {from:DateTime} AND ts <= {to:DateTime}
GROUP BY t ORDER BY t"""),

    ("Events by country", """
SELECT toStartOfInterval(ts, INTERVAL {rounding:UInt32} SECOND) AS t,
       countIf(country = 'US') AS US, countIf(country = 'IN') AS IN,
       countIf(country = 'DE') AS DE, countIf(country = 'BR') AS BR,
       countIf(country = 'JP') AS JP, countIf(country = 'GB') AS GB
FROM learn.events WHERE ts >= {from:DateTime} AND ts <= {to:DateTime}
GROUP BY t ORDER BY t"""),

    ("Page duration p50 / p95 / p99 (ms)", """
SELECT toStartOfInterval(ts, INTERVAL {rounding:UInt32} SECOND) AS t,
       quantile(0.5)(duration_ms) AS p50, quantile(0.95)(duration_ms) AS p95,
       quantile(0.99)(duration_ms) AS p99
FROM learn.events WHERE ts >= {from:DateTime} AND ts <= {to:DateTime}
GROUP BY t ORDER BY t"""),

    ("Unique users per interval (HyperLogLog)", """
SELECT toStartOfInterval(ts, INTERVAL {rounding:UInt32} SECOND) AS t, uniq(user_id) AS users
FROM learn.events WHERE ts >= {from:DateTime} AND ts <= {to:DateTime}
GROUP BY t ORDER BY t"""),

    ("Events by device", """
SELECT toStartOfInterval(ts, INTERVAL {rounding:UInt32} SECOND) AS t,
       countIf(device = 'mobile') AS mobile, countIf(device = 'desktop') AS desktop,
       countIf(device = 'tablet') AS tablet
FROM learn.events WHERE ts >= {from:DateTime} AND ts <= {to:DateTime}
GROUP BY t ORDER BY t"""),
]

state = {
    "host": "http://localhost:8123/",
    "user": "default",
    "queries": [{"title": t, "query": q.strip()} for t, q in CHARTS],
    # The generated data lives in 2024; without this the dashboard looks at "last 24h" and is empty.
    "params": {"rounding": "3600", "seconds": "86400",
               "from": "2024-01-01 00:00:00", "to": "2024-04-30 00:00:00"},
    "search_query": "",
    "customized": True,
}

if __name__ == "__main__":
    hash_ = base64.b64encode(json.dumps(state).encode()).decode()
    print("http://localhost:8123/dashboard#" + hash_)
