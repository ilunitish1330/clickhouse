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

    # password change
    assert ceo.post("/api/me/password", json={"current": "nope", "new": "Another-pass-2"}).status_code == 400
    assert ceo.post("/api/me/password", json={"current": PW, "new": "Another-pass-2"}).status_code == 200
    assert not login("ceo", "Another-pass-2").get("/api/me").json()["must_change"]

    server.ch("DROP DATABASE puc_app_test")
    print("ok")


if __name__ == "__main__":
    main()
