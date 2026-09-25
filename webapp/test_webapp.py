#!/usr/bin/env python3
"""Role and data-scope checks for the web app, against the real ClickHouse `ub` data.

    python3 webapp/test_webapp.py     # needs ClickHouse with ub loaded; uses its own users database (puc_app_test)

Every rule is checked at the API, where it is enforced -- the browser only
hides what the API already refuses.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]
os.environ["APP_DEFAULT_PASSWORD"] = "Test-pass-1"
os.environ["APP_DB"] = "puc_app_test"  # never touch the real users

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

server.ch("DROP DATABASE IF EXISTS puc_app_test")  # fresh users, seeded on startup
PW = "Test-pass-1"


def login(user: str, pw: str = PW) -> TestClient:
    c = TestClient(server.app)
    c.__enter__()  # runs startup
    r = c.post("/api/login", json={"username": user, "password": pw})
    assert r.status_code == 200, (user, r.text)
    return c


def revenue(c, page="executive", **params):
    return c.get(f"/api/page/{page}", params={"period": "2026-01-01", **params}).json()["tiles"][0]["value"]


def main() -> None:
    anon = TestClient(server.app); anon.__enter__()
    assert anon.get("/api/me").status_code == 401
    assert anon.get("/api/page/executive").status_code == 401
    assert anon.post("/api/login", json={"username": "ceo", "password": "wrong"}).status_code == 401

    ceo = login("ceo")
    me = ceo.get("/api/me").json()
    assert me["must_change"] and len(me["pages"]) == 10 and not me["can_load"]
    total = revenue(ceo)
    islands = {r["label"] for r in ceo.get("/api/page/executive", params={"period": "2026-01-01"}).json()["charts"][1]["rows"]}
    assert islands == {"Mahe Island", "Praslin Island", "La Digue Island"}

    # a regional manager sees only their island, whatever they ask for
    pr = login("praslin.manager")
    praslin = revenue(pr)
    assert 0 < praslin < total
    assert revenue(pr, region=1) == praslin, "asking for Mahe must still return Praslin"
    rows = pr.get("/api/page/executive", params={"period": "2026-01-01"}).json()["charts"][1]["rows"]
    assert {r["label"] for r in rows} == {"Praslin Island"}
    assert pr.get("/api/me").json()["filters"]["region_locked"]
    assert revenue(ceo, region=2) == praslin, "the CEO filtering to Praslin sees the same number"

    # a division manager sees only their utilities
    el = login("elec.manager")
    assert el.get("/api/page/water").status_code == 403
    assert el.get("/api/page/network").status_code == 403
    assert {r["label"] for r in el.get("/api/page/executive").json()["charts"][0]["rows"]} == {"Electricity"}
    assert revenue(el, utility=3) == revenue(el), "asking for water must still return electricity only"
    wm = login("water.manager")
    assert {r["label"] for r in wm.get("/api/page/executive").json()["charts"][0]["rows"]} <= {"Water", "Sewerage"}
    assert abs(revenue(el) + revenue(wm) - total) < 1, "electricity + water + sewerage = everything"

    # page lists per role
    op = login("operator")
    assert op.get("/api/page/executive").status_code == 403 and op.get("/api/me").json()["pages"] == []
    svc = login("service")
    assert svc.get("/api/page/executive").status_code == 403 and svc.get("/api/page/customers").status_code == 200
    assert svc.post("/api/rebuild").status_code == 403
    aud = login("auditor")
    assert aud.get("/api/loads").status_code == 200 and aud.post("/api/rebuild").status_code == 403

    # account numbers: masked for executives, visible to customer service
    top = lambda c: c.get("/api/page/customers", params={"period": "2026-01-01"}).json()["charts"][0]["rows"][0]["label"]
    assert "•" in top(ceo) and "•" not in top(svc)

    # nothing from a query string reaches SQL unchecked
    for bad in ["2026-01-01' OR 1=1 --", "x", "2026-13-01"]:
        r = ceo.get("/api/page/executive", params={"period": bad})
        assert r.status_code == 200 and r.json()["tiles"][0]["value"] > total, "bad period falls back to all periods"
    assert ceo.get("/api/page/executive", params={"region": "1 OR 1=1"}).status_code == 422
    assert ceo.get("/api/page/nope").status_code == 404

    # user admin: only the administrator; a new user can sign in; a disabled one cannot
    assert ceo.get("/api/users").status_code == 403
    adm = login("admin")
    assert adm.post("/api/users", json={"username": "la.digue.cs", "full_name": "CS La Digue", "role": "customer_service",
                                        "region_code": 3, "password": "Welcome-123"}).status_code == 200
    cs = login("la.digue.cs", "Welcome-123")
    rows = cs.get("/api/page/consumption", params={"period": "2026-01-01"}).json()["charts"][2]["rows"]
    assert {r["label"] for r in rows} == {"La Digue Island"}
    assert adm.post("/api/users", json={"username": "x", "full_name": "x", "role": "regional_manager",
                                        "region_code": 0, "password": "Welcome-123"}).status_code == 400
    assert adm.post("/api/users", json={"username": "la.digue.cs", "full_name": "CS La Digue", "role": "customer_service",
                                        "region_code": 3, "active": False}).status_code == 200
    assert cs.get("/api/me").status_code == 401, "a disabled account's session stops working"
    assert adm.post("/api/users", json={"username": "admin", "full_name": "a", "role": "executive",
                                        "region_code": 0}).status_code == 400, "no self-demotion"

    # periods: one month, a from-to range, or everything combined
    months = [p["period"] for p in ceo.get("/api/me").json()["filters"]["periods"]]
    first, last = months[0], months[-1]
    each = sum(revenue(ceo, period=m) for m in months)
    both = ceo.get("/api/page/executive", params={"pfrom": last, "pto": first}).json()["tiles"][0]["value"]
    assert abs(both - each) < 0.01, "a range (in either order) adds up its months"
    assert revenue(ceo, period="", pfrom=last, pto=last) == revenue(ceo, period=last), "a one-month range is that month"

    # review: only reviewers; an edit waits until it is finalized, then rebuilds; Reviewed makes it Final.
    # All on a throwaway test month (test_formula.py builds it), never on the real months.
    import test_formula
    test_formula.drop_batch()
    test_formula.make_batch()
    b, last = test_formula.BATCH, test_formula.PERIOD
    assert ceo.get("/api/review/batches").status_code == 403
    rv = login("reviewer")
    assert any(x["batch_id"] == b for x in rv.get("/api/review/batches").json()["batches"])
    row = rv.get(f"/api/review/{b}/rows", params={"size": 10}).json()["rows"][0]
    line, amount = row["line_no"], row["values"]["AMOUNT"]
    assert rv.post(f"/api/review/{b}/edit", json={"line_no": line, "changes": {"AMOUNT": "abc"}}).status_code == 400
    assert rv.post(f"/api/review/{b}/edit", json={"line_no": line, "changes": {"BatchId": "x"}}).status_code == 400
    before = revenue(ceo, period=last)

    def finalize():
        job = rv.post(f"/api/review/{b}/finalize").json()["job"]
        for _ in range(600):
            j = rv.get(f"/api/load/{job}").json()
            if j["status"] != "running":
                assert j["status"] == "done", "\n".join(j["log"][-15:])
                return
            __import__("time").sleep(0.2)
        raise AssertionError("finalize did not finish")

    def status():
        return next(x for x in rv.get("/api/review/batches").json()["batches"] if x["batch_id"] == b)

    assert rv.post(f"/api/review/{b}/edit", json={"line_no": line, "changes": {"AMOUNT": str(float(amount) + 1000)}}).json()["ok"]
    assert status()["pending"] == 1 and revenue(ceo, period=last) == before, "a pending edit is not on the dashboards"
    assert rv.post(f"/api/review/{b}/reviewed").status_code == 400, "cannot review with pending edits"
    rev = status()["revision"]
    finalize()
    assert abs(revenue(ceo, period=last) - before - 1000) < 0.01, "finalize rebuilds the dashboards"
    assert status()["status"] == "draft" and status()["revision"] == rev + 1 and status()["pending"] == 0
    assert rv.post(f"/api/review/{b}/reviewed").json()["ok"] and status()["status"] == "final"
    assert ceo.get("/api/page/executive", params={"period": last}).json()["review"][0]["status"] == "final"
    # put it back: editing Final data starts the next Draft revision
    assert rv.post(f"/api/review/{b}/edit", json={"line_no": line, "changes": {"AMOUNT": amount}}).json()["ok"]
    finalize()
    assert revenue(ceo, period=last) == before and status()["status"] == "draft" and status()["revision"] == rev + 2
    assert rv.post(f"/api/review/{b}/reviewed").json()["ok"]
    hist = rv.get(f"/api/review/{b}/history").json()["history"]
    assert {"update", "finalize", "reviewed"} <= {h["action"] for h in hist}

    # formula builder: rule + action + formulas, compiled safely; a preview first; applying makes pending edits
    assert ceo.post(f"/api/review/{b}/formula/preview", json={}).status_code == 403
    praslin_water = {"combine": "and", "rules": [{"field": "REGION", "op": "eq", "value": "2"},
                                                 {"field": "UTILITYTYPE", "op": "eq", "value": "3"}]}
    spec = {"rule": praslin_water, "action": "update", "sets": [{"field": "AMOUNT", "formula": "[Amount] *"}]}
    r = rv.post(f"/api/review/{b}/formula/preview", json=spec)
    assert r.status_code == 400 and r.json()["field"] == "AMOUNT" and r.json()["pos"] == 10, r.text
    for evil in ["1; DROP TABLE ub.raw_rows", "sleep(3)", "[Amount]) OR (1", "file('/etc/passwd')"]:
        spec["sets"][0]["formula"] = evil
        assert rv.post(f"/api/review/{b}/formula/preview", json=spec).status_code == 400, evil
    evil_rule = {"rules": [{"field": "CUSTID", "op": "eq", "value": "x' OR '1'='1"}]}
    assert rv.post(f"/api/review/{b}/formula/preview", json={"rule": evil_rule, "action": "delete"}).json()["matched"] == 0
    assert rv.post(f"/api/review/{b}/formula/preview",
                   json={"rule": {"rules": [{"field": "nope", "op": "eq", "value": 1}]}, "action": "delete"}).status_code == 400
    spec["sets"][0]["formula"] = "ROUND([Amount] * 1.1, 2)"
    p = rv.post(f"/api/review/{b}/formula/preview", json=spec).json()
    island_rows = rv.get(f"/api/review/{b}/rows", params={"rule": __import__("json").dumps(praslin_water), "size": 10}).json()["total"]
    assert p["matched"] == island_rows > 0 and 0 < p["affected"] <= p["matched"]
    assert abs(p["amount_after"] - p["amount_before"] * 1.1) < p["matched"] * 0.01, "10% more, give or take rounding"
    bad = rv.post(f"/api/review/{b}/formula/preview",
                  json={"rule": praslin_water, "action": "update", "sets": [{"field": "REGION", "formula": "7"}]}).json()
    assert bad["invalid"] and rv.post(f"/api/review/{b}/formula/apply", json={"rule": praslin_water, "action": "update",
                                      "sets": [{"field": "REGION", "formula": "7"}]}).status_code == 400
    assert rv.post(f"/api/review/{b}/formula/apply", json=spec).json()["affected"] == p["affected"]
    assert status()["pending"] == p["affected"] and revenue(ceo, period=last) == before, "applied, but not on the dashboards"
    big = {"combine": "and", "rules": [{"field": "REGION", "op": "eq", "value": "3"}, {"field": "AMOUNT", "op": "gt", "value": "3000"}]}
    copies = rv.post(f"/api/review/{b}/formula/apply", json={"rule": big, "action": "copy",
                     "sets": [{"field": "CUSTID", "formula": '"TEST-" & [Customer]'}]}).json()["affected"]
    assert copies > 0 and status()["pending"] == p["affected"] + copies
    assert rv.post(f"/api/review/{b}/formula/apply", json={"rule": {"rules": [{"field": "CUSTID", "op": "starts", "value": "TEST-"}]},
                                                          "action": "delete"}).json()["affected"] == copies
    assert status()["pending"] == p["affected"], "deleting the copies drops them from the pending edits"
    assert "formula" in {h["action"] for h in rv.get(f"/api/review/{b}/history").json()["history"]}
    assert rv.post(f"/api/review/{b}/discard").json()["discarded"] == p["affected"] and status()["pending"] == 0
    assert rv.post("/api/review/formulas/save", json={"name": "test: praslin water +10%", "spec": spec}).json()["ok"]
    assert any(x["name"] == "test: praslin water +10%" for x in rv.get("/api/review/formulas/saved").json()["saved"])
    rv.post("/api/review/formulas/forget", json={"name": "test: praslin water +10%"})
    assert not any(x["name"].startswith("test:") for x in rv.get("/api/review/formulas/saved").json()["saved"])

    # password change
    assert ceo.post("/api/me/password", json={"current": "nope", "new": "Another-pass-2"}).status_code == 400
    assert ceo.post("/api/me/password", json={"current": PW, "new": "Another-pass-2"}).status_code == 200
    assert not login("ceo", "Another-pass-2").get("/api/me").json()["must_change"]

    server.ch("DROP DATABASE puc_app_test")
    print("ok")


if __name__ == "__main__":
    try:
        main()
    finally:
        import test_formula
        test_formula.drop_batch()  # the test month goes, whatever happened
