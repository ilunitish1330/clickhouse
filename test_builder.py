"""Smoke check for the table builder: python3 test_builder.py

Preloads the MSSQL schema cache with a slice of the real SALESLINE columns, so the
DDL rules can be checked without a database on either side. Only the last check,
which actually creates the table, needs ClickHouse running.
"""
import json
from decimal import Decimal

from fastapi.testclient import TestClient

import app
from app import app as api

c = TestClient(api)

app._schema_cache["SALESLINE"] = [
    {"name": "DATAAREAID", "type": "String", "mssql_type": "nvarchar"},
    {"name": "SALESID", "type": "String", "mssql_type": "nvarchar"},
    {"name": "ITEMID", "type": "String", "mssql_type": "nvarchar"},
    {"name": "LINEAMOUNT", "type": "Decimal(32, 16)", "mssql_type": "numeric"},
    {"name": "SALESQTY", "type": "Decimal(32, 16)", "mssql_type": "numeric"},
    {"name": "CREATEDDATETIME", "type": "DateTime64(3)", "mssql_type": "datetime"},
]


def post(**spec):
    return c.post("/api/build", json={"source": "SALESLINE", **spec})


def check():
    # Two aggregates off the same column is the point of the UI — one row each.
    r = post(name="agg_test_builder", cols=[
        {"col": "DATAAREAID", "role": "dimension"},
        {"col": "ITEMID", "role": "dimension"},
        {"col": "LINEAMOUNT", "role": "aggregate", "agg": "sum"},
        {"col": "LINEAMOUNT", "role": "aggregate", "agg": "avg"},
        {"col": "SALESID", "role": "aggregate", "agg": "uniq"},
        {"col": "CREATEDDATETIME", "role": "fact"},
    ])
    ddl = r.json()["ddl"]
    assert "`LINEAMOUNT_sum` SimpleAggregateFunction(sum, Float64)" in ddl, ddl
    assert "`LINEAMOUNT_avg` AggregateFunction(avg, Decimal(32, 16))" in ddl, ddl
    assert "`SALESID_uniq` AggregateFunction(uniq, String)" in ddl, ddl
    assert "`CREATEDDATETIME` DateTime64(3)" in ddl, ddl
    assert "ENGINE = AggregatingMergeTree" in ddl and \
           "ORDER BY (DATAAREAID, ITEMID)" in ddl, ddl

    # State columns are flagged with the function that reads them back.
    warns = " ".join(r.json()["warnings"])
    assert "LINEAMOUNT_avg -> avgMerge(LINEAMOUNT_avg)" in warns, warns
    # ...but that spec keys on DATAAREAID, so no currency warning belongs on it.
    assert "not a dimension" not in warns, warns

    # Money aggregated without DATAAREAID mixes currencies — warn, never block.
    mixed = post(name="t", cols=[{"col": "ITEMID", "role": "dimension"},
                                 {"col": "LINEAMOUNT", "role": "aggregate", "agg": "sum"}])
    assert "DATAAREAID is not a dimension" in " ".join(mixed.json()["warnings"]), mixed.json()
    keyed = post(name="t", cols=[{"col": "DATAAREAID", "role": "dimension"},
                                 {"col": "LINEAMOUNT", "role": "aggregate", "agg": "sum"}])
    assert not keyed.json()["warnings"], keyed.json()

    # No aggregate -> a plain MergeTree, dimensions still make the key.
    plain = post(name="t", cols=[{"col": "DATAAREAID", "role": "dimension"},
                                 {"col": "LINEAMOUNT", "role": "measure"}]).json()["ddl"]
    assert "ENGINE = MergeTree" in plain, plain

    # Everything the page can send is checked against the source schema.
    assert post(name="t", cols=[{"col": "NOPE", "role": "dimension"}]).status_code == 400
    assert post(name="t; DROP TABLE ax.fact_sales", cols=[]).status_code == 400
    assert c.post("/api/build", json={"name": "t", "source": "SYSUSERS",
                                      "cols": []}).status_code == 400
    assert post(name="t", cols=[{"col": "LINEAMOUNT", "role": "aggregate",
                                 "agg": "sum(1)--"}]).status_code == 400
    assert post(name="t", cols=[{"col": "DATAAREAID", "role": "dimension"},
                                {"col": "DATAAREAID", "role": "fact"}]).status_code == 400

    # ClickHouse must accept the DDL — the only check that needs a live server.
    made = post(name="agg_test_builder", create=True, cols=[
        {"col": "DATAAREAID", "role": "dimension"},
        {"col": "LINEAMOUNT", "role": "aggregate", "agg": "sum"}])
    assert made.status_code == 200, made.text
    # replace=True is the redesign loop: the second create must not fail on "exists".
    again = post(name="agg_test_builder", create=True, replace=True, cols=[
        {"col": "DATAAREAID", "role": "dimension"},
        {"col": "SALESID", "role": "aggregate", "agg": "uniq"}])
    assert again.status_code == 200, again.text
    # The load: MSSQL hands over raw rows, ClickHouse does the aggregating, because
    # a uniq/avg merge state cannot be computed in T-SQL.
    spec = app.TableSpec(name="agg_x", source="SALESLINE", cols=[
        {"col": "DATAAREAID", "role": "dimension"},
        {"col": "CREATEDDATETIME", "role": "fact"},
        {"col": "LINEAMOUNT", "role": "aggregate", "agg": "sum"},
        {"col": "LINEAMOUNT", "role": "aggregate", "agg": "avg"},
        {"col": "SALESID", "role": "aggregate", "agg": "uniq"},
    ])
    types = {c["name"]: c["type"] for c in app._schema_cache["SALESLINE"]}
    raw, src, ins = app.load_queries(spec, types)
    # each source column is pulled once, however many aggregates hang off it
    assert raw == ["DATAAREAID", "CREATEDDATETIME", "LINEAMOUNT", "SALESID"], raw
    assert src == ("SELECT DATAAREAID, CREATEDDATETIME, LINEAMOUNT, SALESID "
                   "FROM SALESLINE"), src
    assert "sum(toFloat64(`LINEAMOUNT`)) AS `LINEAMOUNT_sum`" in ins, ins
    assert "avgState(`LINEAMOUNT`) AS `LINEAMOUNT_avg`" in ins, ins
    assert "uniqState(`SALESID`) AS `SALESID_uniq`" in ins, ins
    # the grain is every non-aggregate column, facts included
    assert "GROUP BY `DATAAREAID`, `CREATEDDATETIME`" in ins, ins
    assert "input('`DATAAREAID` String, `CREATEDDATETIME` DateTime64(3), " in ins, ins

    # No aggregates -> a straight row copy, no input() wrapper and no GROUP BY.
    flat = app.load_queries(app.TableSpec(name="t", source="SALESLINE", cols=[
        {"col": "SALESID", "role": "dimension"},
        {"col": "LINEAMOUNT", "role": "measure"}]), types)[2]
    assert flat == "INSERT INTO ax.t FORMAT JSONEachRow", flat

    # The insert itself, round-tripped through ClickHouse: pymssql hands back Decimal
    # and datetime objects, which reach JSONEachRow as strings — this is the check that
    # they parse into Decimal(32,16)/DateTime64 columns and aggregate to the right numbers.
    probe = app.TableSpec(name="agg_test_probe", source="SALESLINE", create=True,
                          replace=True, cols=[
        {"col": "DATAAREAID", "role": "dimension"},
        {"col": "LINEAMOUNT", "role": "aggregate", "agg": "sum"},
        {"col": "LINEAMOUNT", "role": "aggregate", "agg": "avg"},
        {"col": "SALESID", "role": "aggregate", "agg": "uniq"}])
    app.build(probe)
    raw, _, ins = app.load_queries(probe, types)
    vals = {"DATAAREAID": ["usmf", "usmf", "inmf"], "SALESID": ["SO-1", "SO-2", "SO-3"],
            "LINEAMOUNT": [Decimal("10.5"), Decimal("4.25"), Decimal("100")]}
    body = b"\n".join(json.dumps({c: vals[c][i] for c in raw}, default=str).encode()
                      for i in range(3))
    r = app.client.post(app.CH, params={"query": ins}, content=body)
    assert r.status_code == 200, r.text[:300]
    got = app.client.post(app.CH, content=b"""
SELECT DATAAREAID, sum(LINEAMOUNT_sum), avgMerge(LINEAMOUNT_avg), uniqMerge(SALESID_uniq)
FROM ax.agg_test_probe GROUP BY DATAAREAID ORDER BY DATAAREAID""").text
    assert got == "inmf\t100\t100\t1\nusmf\t14.75\t7.375\t2\n", repr(got)

    app.client.post(app.CH, content=b"DROP TABLE ax.agg_test_builder")
    app.client.post(app.CH, content=b"DROP TABLE ax.agg_test_probe")
    print("ok")


if __name__ == "__main__":
    check()
