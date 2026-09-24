"""Smoke check: python3 test_app.py (needs the ClickHouse server running)."""
from fastapi.testclient import TestClient

from app import app

c = TestClient(app)
RANGE = {"frm": "2024-01-01 00:00:00", "to": "2024-01-08 00:00:00"}


def check():
    s = c.get("/api/summary", params=RANGE).json()
    assert s["data"]["events"] > 0 and s["stats"]["read_rows"] > 0, s

    # The index prefix earns its keep: filtering on country (first ORDER BY key)
    # must read strictly fewer rows than the same query without it.
    one = c.get("/api/summary", params={**RANGE, "country": "US"}).json()
    assert one["stats"]["read_rows"] < s["stats"]["read_rows"], (one["stats"], s["stats"])

    ts = c.get("/api/timeseries", params={**RANGE, "bucket": 3600}).json()
    # 168 hourly buckets, +1 when the server timezone isn't a whole-hour offset
    # from UTC: toStartOfInterval aligns to UTC, so both ends land mid-bucket.
    assert len(ts["data"]) in (7 * 24, 7 * 24 + 1), len(ts["data"])
    assert all(r["mobile"] + r["desktop"] + r["tablet"] > 0 for r in ts["data"])

    for d in ("country", "device", "url"):
        b = c.get("/api/breakdown", params={**RANGE, "dim": d, "limit": 5}).json()
        events = [r["events"] for r in b["data"]]
        assert events == sorted(events, reverse=True) and events[0] > 0, b
    assert c.get("/api/breakdown", params={**RANGE, "dim": "revenue"}).status_code == 400

    dims = {r["name"]: r for r in c.get("/api/dimensions").json()["data"]}
    # LowCardinality columns compress hard, random user_id barely at all — lesson 4.
    assert dims["country"]["distinct"] == 6 and dims["device"]["distinct"] == 3, dims
    assert dims["country"]["ratio"] > dims["user_id"]["ratio"], dims

    assert c.get("/api/values", params={"dim": "country"}).json() == \
        ["BR", "DE", "GB", "IN", "JP", "US"]
    assert c.get("/api/values", params={"dim": "device"}).json() == \
        ["desktop", "mobile", "tablet"]
    print("ok")


if __name__ == "__main__":
    check()
