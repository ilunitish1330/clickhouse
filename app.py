#!/usr/bin/env python3
"""Demo app over learn.events. Talks to ClickHouse via its HTTP interface.

ponytail: no clickhouse driver dependency — the HTTP interface returns JSON and
reports scan cost in the X-ClickHouse-Summary header, which is the whole point of
the app: every panel shows the SQL it ran and how many rows that cost.

    uvicorn app:app --port 8010     (or just: python3 app.py)
"""
import json
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

CH = "http://localhost:8123/"
app = FastAPI(title="ClickHouse demo")
client = httpx.Client(timeout=30.0)


def run(sql: str, **params):
    """Execute SQL, return (rows, stats). Params are passed as ClickHouse query
    parameters — {name:Type} in SQL — so user input is never string-concatenated."""
    r = client.post(
        CH,
        params={"default_format": "JSON", **{f"param_{k}": v for k, v in params.items()}},
        content=sql.encode(),
    )
    if r.status_code != 200:
        raise HTTPException(502, r.text[:500])
    summary = json.loads(r.headers.get("X-ClickHouse-Summary", "{}"))
    body = r.json()
    return body["data"], {
        "sql": sql.strip(),
        "read_rows": int(summary.get("read_rows", 0)),
        "read_bytes": int(summary.get("read_bytes", 0)),
        "elapsed_ms": round(body.get("statistics", {}).get("elapsed", 0) * 1000, 1),
    }


# Every query filters on ts, country and device: country leads the ORDER BY, so the
# country filter is an index prefix hit, ts is the second key part, and device is not
# in the key at all — a pure scan filter. The cost line under each panel shows it.
WHERE = """ts >= {from:DateTime} AND ts < {to:DateTime}
  AND ({country:String} = '' OR country = {country:String})
  AND ({device:String}  = '' OR device  = {device:String})"""

DIMS = ("country", "device", "url")  # the columns you're allowed to slice by


def filters(frm, to, country, device, **extra):
    return {"from": frm, "to": to, "country": country, "device": device, **extra}


@app.get("/api/summary")
def summary(frm: str, to: str, country: str = "", device: str = ""):
    rows, stats = run(f"""
SELECT count() AS events, uniq(user_id) AS users, sum(revenue) AS revenue,
       quantile(0.95)(duration_ms) AS p95_ms
FROM learn.events WHERE {WHERE}""", **filters(frm, to, country, device))
    return {"data": rows[0], "stats": stats}


@app.get("/api/timeseries")
def timeseries(frm: str, to: str, country: str = "", device: str = "", bucket: int = 3600):
    rows, stats = run(f"""
SELECT toStartOfInterval(ts, INTERVAL {{bucket:UInt32}} SECOND) AS t,
       countIf(device = 'mobile')  AS mobile,
       countIf(device = 'desktop') AS desktop,
       countIf(device = 'tablet')  AS tablet
FROM learn.events WHERE {WHERE}
GROUP BY t ORDER BY t""", **filters(frm, to, country, device, bucket=bucket))
    return {"data": rows, "stats": stats}


@app.get("/api/breakdown")
def breakdown(dim: str, frm: str, to: str, country: str = "", device: str = "", limit: int = 10):
    """One panel for any dimension. `dim` is whitelisted, not parameterised: a column
    name can't be a query parameter, so the whitelist is what keeps it injection-free."""
    if dim not in DIMS:
        raise HTTPException(400, f"dim must be one of {DIMS}")
    rows, stats = run(f"""
SELECT {dim} AS value, count() AS events, uniq(user_id) AS users,
       sum(revenue) AS revenue, round(avg(duration_ms)) AS avg_ms
FROM learn.events WHERE {WHERE}
GROUP BY value ORDER BY events DESC LIMIT {{limit:UInt32}}""",
                      **filters(frm, to, country, device, limit=limit))
    return {"data": rows, "stats": stats}


@app.get("/api/dimensions")
def dimensions():
    """The shape of the table itself: every column, its type, distinct values, and what
    it costs on disk. Lesson 1 and lesson 4 as a table you can look at."""
    cols, _ = run("""
SELECT name, type, data_compressed_bytes AS compressed,
       data_uncompressed_bytes AS uncompressed,
       round(data_uncompressed_bytes / greatest(data_compressed_bytes, 1), 1) AS ratio
FROM system.columns WHERE database = 'learn' AND table = 'events'""")
    # One pass over the table for all cardinalities — uniq() is HyperLogLog (lesson 5).
    uniqs, stats = run("SELECT " + ", ".join(
        f"uniq({c['name']}) AS `{c['name']}`" for c in cols) + " FROM learn.events")
    for c in cols:
        c["distinct"] = uniqs[0][c["name"]]
    return {"data": cols, "stats": stats}


@app.get("/api/values")
def values(dim: str):
    """Distinct values of a dimension, for the filter dropdowns."""
    if dim not in DIMS:
        raise HTTPException(400, f"dim must be one of {DIMS}")
    rows, _ = run(f"SELECT DISTINCT {dim} AS v FROM learn.events ORDER BY v LIMIT 1000")
    return [r["v"] for r in rows]


@app.get("/api/sql")
def sql(q: str):
    """Free-form SQL over ax.*, for the /ask console.

    ponytail: safety is ClickHouse's own readonly=1 setting, not SQL parsing. A
    blacklist of words like DROP is trivially bypassed; readonly=1 rejects every
    write and DDL at the server, which is the only place that can be sure."""
    r = client.post(CH, params={"default_format": "JSON", "readonly": "1",
                                "max_result_rows": "500", "result_overflow_mode": "break"},
                    content=q.encode())
    if r.status_code != 200:
        raise HTTPException(502, r.text[:500])
    body = r.json()
    summary = json.loads(r.headers.get("X-ClickHouse-Summary", "{}"))
    return {"data": body["data"], "cols": [c["name"] for c in body["meta"]],
            "stats": {"sql": q, "read_rows": int(summary.get("read_rows", 0)),
                      "read_bytes": int(summary.get("read_bytes", 0)),
                      "elapsed_ms": round(body.get("statistics", {}).get("elapsed", 0) * 1000, 1)}}


SCHEMA_SQL = """
SELECT table, groupArray(concat(name, ' ', type)) AS cols
FROM system.columns WHERE database = 'ax' GROUP BY table ORDER BY table"""

NL_SYSTEM = """You write ClickHouse SQL against the `ax` database, which holds Dynamics AX
2012 data. Reply with ONE SELECT query and nothing else — no prose, no markdown fence.

Schema:
{schema}

Rules:
- Every table is a ReplacingMergeTree: add FINAL to each table in the FROM/JOIN
  (`FROM ax.customers FINAL`) or stale duplicate rows come back.
- data_area is the AX legal entity (inmf=India/INR, rumf=Russia/RUB, usmf=US/USD).
  Money columns are in that entity's own currency, so GROUP BY data_area or filter
  to one rather than summing across them. There is no FX table.
- purchase_orders is header-only and has no amount — PO money is
  purchase_lines.line_amount, joined USING (data_area, purch_id).
- Join invoices to parties on both data_area and the account id.
- LIMIT 100 unless the question implies otherwise."""

_SCHEMA = None


def schema_text() -> str:
    """Read once per process — new DDL needs a restart, which is cheaper than a
    system.columns query on every question."""
    global _SCHEMA
    if _SCHEMA is None:
        rows, _ = run(SCHEMA_SQL)
        _SCHEMA = "\n".join(f"{r['table']}({', '.join(r['cols'])})" for r in rows)
    return _SCHEMA


@app.get("/api/ask")
def ask_nl(q: str):
    """English -> SQL -> rows. Returns the SQL as well: it goes back into the editor
    so you can see what actually ran and fix it, rather than trusting a hidden query."""
    import anthropic

    try:
        msg = anthropic.Anthropic().messages.create(
            model="claude-opus-5",
            max_tokens=2000,
            system=NL_SYSTEM.format(schema=schema_text()),
            messages=[{"role": "user", "content": q}],
        )
    # No key at all is a TypeError from the constructor, a bad key is
    # AuthenticationError — same fix for whoever is looking at the browser.
    except (anthropic.AuthenticationError, TypeError):
        raise HTTPException(401, "ANTHROPIC_API_KEY is not set for the server process")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, e.message)

    if msg.stop_reason == "refusal":
        raise HTTPException(400, "the model declined to answer that")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    # ponytail: strip a fence if one shows up anyway; no parser for a 3-char marker.
    return sql(text.removeprefix("```sql").removeprefix("```").removesuffix("```").strip())


ASK_HTML = """<!doctype html><meta charset=utf-8><title>ask ax</title>
<style>body{font:14px/1.5 system-ui;margin:2rem;max-width:1100px}
textarea{width:100%;height:7rem;font:13px/1.4 ui-monospace}
table{border-collapse:collapse;margin-top:1rem}td,th{border:1px solid #ccc;padding:2px 8px}
th{background:#f4f4f4;text-align:left}td{text-align:right}td:first-child{text-align:left}
#err{color:#b00;white-space:pre-wrap}#cost{color:#666}
button{padding:.4rem 1rem}details{margin:.5rem 0;color:#555}</style>
<h1>ask ax</h1>
<details><summary>tables</summary><pre id=schema>loading…</pre></details>
<p><input id=nl size=70 placeholder="ask in English — e.g. top 10 vendors by spend in inmf">
<button onclick=askNL()>Ask</button></p>
<textarea id=q>SELECT o.vendor_name, round(sum(l.line_amount)) AS ordered
FROM ax.purchase_lines l FINAL
JOIN ax.purchase_orders o FINAL USING (data_area, purch_id)
WHERE o.data_area = 'inmf'
GROUP BY 1 ORDER BY ordered DESC LIMIT 10</textarea>
<p><button onclick=go()>Run</button> <span id=cost></span></p>
<div id=err></div><div id=out></div>
<script>
function render(j){
  cost.textContent=`${j.data.length} rows · read ${j.stats.read_rows.toLocaleString()} rows`
    +` (${(j.stats.read_bytes/1e6).toFixed(1)} MB) in ${j.stats.elapsed_ms} ms`;
  const th=j.cols.map(c=>`<th>${c}</th>`).join('');
  const tr=j.data.map(r=>'<tr>'+j.cols.map(c=>`<td>${r[c]??''}</td>`).join('')+'</tr>').join('');
  out.innerHTML=`<table><tr>${th}</tr>${tr}</table>`;
}
async function call(path,param){
  err.textContent=''; out.innerHTML=''; cost.textContent='running…';
  const r=await fetch(path+'?q='+encodeURIComponent(param));
  const j=await r.json();
  if(!r.ok){cost.textContent='';err.textContent=j.detail;return null}
  render(j); return j;
}
const go=()=>call('/api/sql',q.value);
// The generated SQL lands in the editor: you see what ran, and can edit and re-run it.
const askNL=async()=>{const j=await call('/api/ask',nl.value); if(j) q.value=j.stats.sql};
fetch('/api/sql?q='+encodeURIComponent(
  "SELECT table, groupArray(name) AS cols FROM system.columns WHERE database='ax' GROUP BY table"))
 .then(r=>r.json()).then(j=>schema.textContent=
   j.data.map(r=>r.table+'\\n  '+r.cols.join(', ')).join('\\n\\n'));
q.addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))go()});
nl.addEventListener('keydown',e=>{if(e.key==='Enter')askNL()});
</script>"""


@app.get("/ask", response_class=HTMLResponse)
def ask():
    return ASK_HTML


# ============ table builder ============
# Tables and columns come from MSSQL (INFORMATION_SCHEMA), not from ClickHouse: the
# point is to design the ClickHouse table before it exists, off the AX source.
# The browser sends a spec (source table + role per column), never SQL: the DDL is
# built here from that schema, so nothing the page posts can reach the server as
# a statement. Roles map to physical shapes:
#   dimension  -> plain column, part of ORDER BY (the grain)
#   fact/measure -> plain column, raw value carried through
#   aggregate  -> rolled-up column named <col>_<agg>, AggregatingMergeTree

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# sum/min/max/any merge by value — SimpleAggregateFunction, readable with no -Merge.
SIMPLE_AGGS = {"sum", "min", "max", "any"}
# these need a merge state: read them with avgMerge()/uniqMerge().
STATE_AGGS = {"avg", "uniq", "uniqExact"}
AGGS = SIMPLE_AGGS | STATE_AGGS | {"count"}
ROLES = {"dimension", "measure", "fact", "aggregate"}


# The four AX tables in scope — same four ax_load.py reads.
SOURCE_TABLES = ("SALESTABLE", "SALESLINE", "CUSTTABLE", "CUSTTRANS")

# ponytail: char/varchar/nvarchar/text and anything unlisted fall through to String,
# and nothing is Nullable — AX writes '' and 1900-01-01, not NULL, and a Nullable
# column cannot lead an ORDER BY. Decimal(32,16) matches ch_schema.sql.
MSSQL_TO_CH = {
    "bigint": "Int64", "int": "Int32", "smallint": "Int16", "tinyint": "UInt8",
    "bit": "UInt8", "float": "Float64", "real": "Float32",
    "decimal": "Decimal(32, 16)", "numeric": "Decimal(32, 16)", "money": "Decimal(32, 16)",
    "date": "Date", "datetime": "DateTime64(3)", "datetime2": "DateTime64(3)",
    "smalldatetime": "DateTime64(3)", "uniqueidentifier": "UUID",
}

_schema_cache: dict[str, list[dict]] = {}

# MSSQL credentials supplied from the UI — falls back to env vars if not set.
_mssql_creds: dict[str, str] = {}

# ClickHouse endpoint supplied from the UI — falls back to the default localhost.
_ch_creds: dict[str, str] = {}  # {"url": ..., "user": ..., "password": ...}


def _mssql_connect():
    """Open an MSSQL connection using UI-supplied creds or env vars."""
    if _mssql_creds:
        import os
        os.environ.setdefault("TDSVER", "7.0")
        import pymssql
        return pymssql.connect(
            server=_mssql_creds["host"],
            user=_mssql_creds["user"],
            password=_mssql_creds["password"],
            database=_mssql_creds["database"],
            port=_mssql_creds.get("port", "1433"),
        )
    import ax_load
    try:
        return ax_load.connect()
    except KeyError as e:
        raise HTTPException(500, f"{e.args[0]} is not set — connect via the builder UI "
                                 "or export MSSQL_HOST, MSSQL_USER, MSSQL_PASS, MSSQL_DB.")


class MssqlCreds(BaseModel):
    host: str
    user: str
    password: str
    database: str
    port: str = "1433"


class ClickHouseCreds(BaseModel):
    host: str = "localhost"
    port: str = "8123"
    user: str = ""
    password: str = ""


def _ch_base_url() -> str:
    """The ClickHouse HTTP URL to use — UI-supplied or the default."""
    return _ch_creds.get("url", CH)


def _ch_params() -> dict:
    """Extra query params (user/password) for ClickHouse requests."""
    p = {}
    if _ch_creds.get("user"):
        p["user"] = _ch_creds["user"]
    if _ch_creds.get("password"):
        p["password"] = _ch_creds["password"]
    return p


@app.post("/api/connect")
def connect_mssql(creds: MssqlCreds):
    """Test MSSQL connection and cache credentials for the session."""
    global _mssql_creds
    import os
    os.environ.setdefault("TDSVER", "7.0")
    import pymssql
    try:
        conn = pymssql.connect(
            server=creds.host, user=creds.user, password=creds.password,
            database=creds.database, port=creds.port,
        )
        conn.close()
    except Exception as e:
        raise HTTPException(400, f"Connection failed: {e}")
    _mssql_creds = creds.model_dump()
    _schema_cache.clear()  # force re-read with new creds
    return {"ok": True}


@app.post("/api/connect_ch")
def connect_ch(creds: ClickHouseCreds):
    """Test ClickHouse connection and cache the URL for the session."""
    global _ch_creds
    url = f"http://{creds.host}:{creds.port}/"
    params = {"query": "SELECT 1", "default_format": "JSON"}
    if creds.user:
        params["user"] = creds.user
    if creds.password:
        params["password"] = creds.password
    try:
        r = client.get(url, params=params)
        if r.status_code != 200:
            raise HTTPException(400, f"ClickHouse responded: {r.text[:300]}")
    except httpx.ConnectError as e:
        raise HTTPException(400, f"Cannot reach ClickHouse at {url}: {e}")
    _ch_creds = {"url": url, "user": creds.user, "password": creds.password}
    return {"ok": True}


@app.get("/api/connected")
def connected():
    """Check whether MSSQL credentials are available (UI-supplied or env vars)."""
    import os
    has_env = all(k in os.environ for k in ("MSSQL_HOST", "MSSQL_USER", "MSSQL_PASS", "MSSQL_DB"))
    return {"mssql": bool(_mssql_creds) or has_env, "clickhouse": bool(_ch_creds) or True}


def source_schema() -> dict[str, list[dict]]:
    """{table: [{name, type, mssql_type}]} for the four AX tables, read once."""
    if not _schema_cache:
        conn = _mssql_connect()
        cur = conn.cursor()
        cur.execute("""
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME IN (%s, %s, %s, %s) ORDER BY TABLE_NAME, ORDINAL_POSITION""",
                    SOURCE_TABLES)
        for table, col, typ in cur.fetchall():
            _schema_cache.setdefault(table, []).append(
                {"name": col, "type": MSSQL_TO_CH.get(typ, "String"), "mssql_type": typ})
        conn.close()
    return _schema_cache


@app.get("/api/source")
def source():
    return source_schema()


class ColSpec(BaseModel):
    col: str
    role: str
    agg: str = ""


class TableSpec(BaseModel):
    name: str
    source: str
    cols: list[ColSpec]
    create: bool = False
    replace: bool = False  # drop first — for re-designing a table you just made


def build_ddl(spec: TableSpec) -> tuple[str, list[str]]:
    """Returns the DDL and the warnings that come with it. The warnings are here and
    not in the page because only this side knows the column types."""
    if not IDENT.match(spec.name):
        raise HTTPException(400, "table name must be a plain identifier")
    if spec.source not in SOURCE_TABLES:
        raise HTTPException(400, f"source must be one of {list(SOURCE_TABLES)}")
    types = {c["name"]: c["type"] for c in source_schema()[spec.source]}

    defs, order, agg_table, seen = [], [], False, set()
    for c in spec.cols:
        if c.col not in types:
            raise HTTPException(400, f"{spec.source} has no column {c.col}")
        if c.role not in ROLES:
            raise HTTPException(400, f"role must be one of {sorted(ROLES)}")
        src, name = types[c.col], c.col
        if c.role == "aggregate":
            if c.agg not in AGGS:
                raise HTTPException(400, f"aggregation must be one of {sorted(AGGS)}")
            name, agg_table = f"{c.col}_{c.agg}", True
            if c.agg == "count":
                typ = "SimpleAggregateFunction(sum, UInt64)"  # counts add up on merge
            elif c.agg == "sum":
                typ = "SimpleAggregateFunction(sum, Float64)"
            elif c.agg in SIMPLE_AGGS:
                typ = f"SimpleAggregateFunction({c.agg}, {src})"
            else:
                typ = f"AggregateFunction({c.agg}, {src})"
        else:
            typ = src
            if c.role == "dimension":
                order.append(name)
        if name in seen:
            raise HTTPException(400, f"duplicate column {name}")
        seen.add(name)
        defs.append(f"    `{name}` {typ}")

    if not defs:
        raise HTTPException(400, "pick at least one column")
    engine = "AggregatingMergeTree" if agg_table else "MergeTree"
    ddl = (f"CREATE TABLE ax.{spec.name}\n(\n" + ",\n".join(defs) + "\n)\n"
           f"ENGINE = {engine}\nORDER BY ({', '.join(order) or 'tuple()'})")

    # Warnings, not errors: each one is a legitimate design in some case, and the
    # modelling call is the user's. They exist because the numbers come out wrong
    # quietly, with no complaint from ClickHouse.
    warn = []
    money = [c.col for c in spec.cols
             if c.role == "aggregate" and types[c.col].startswith("Decimal")]
    if money and "DATAAREAID" not in order:
        warn.append(f"{', '.join(money)} is money, and DATAAREAID is not a dimension — "
                    "AX amounts are in each legal entity's own currency, so this sums "
                    "INR + RUB + USD into one number. Add DATAAREAID as a dimension.")
    if agg_table and not order:
        warn.append("No dimensions: every row merges into one. That is a grand total, "
                    "not a roll-up.")
    states = [f"{c.col}_{c.agg} -> {c.agg}Merge({c.col}_{c.agg})" for c in spec.cols
              if c.role == "aggregate" and c.agg in STATE_AGGS]
    if states:
        warn.append("These hold merge states, not numbers — reading them as plain "
                    "columns gives garbage: " + "; ".join(states))
    return ddl, warn


@app.post("/api/build")
def build(spec: TableSpec):
    """Preview the DDL; with create=true, run it against ClickHouse."""
    ddl, warnings = build_ddl(spec)
    if spec.create:
        # A freshly connected ClickHouse has no `ax` database yet, and CREATE TABLE
        # into a missing database is just an error — make it first.
        stmts = [b"CREATE DATABASE IF NOT EXISTS ax"]
        if spec.replace:
            stmts.append(f"DROP TABLE IF EXISTS ax.{spec.name}".encode())
        stmts.append(ddl.encode())
        try:
            for stmt in stmts:
                r = client.post(_ch_base_url(), params=_ch_params(), content=stmt)
                if r.status_code != 200:
                    raise HTTPException(502, r.text[:500])
        except httpx.ConnectError:
            raise HTTPException(502, f"Cannot reach ClickHouse at {_ch_base_url()}")
    return {"ddl": ddl, "created": spec.create, "warnings": warnings}


# ============ loading the table ============
# Raw rows come out of MSSQL and ClickHouse does the aggregating. The alternative --
# GROUP BY on the SQL Server side -- cannot work: uniq/avg columns are merge states,
# and there is no way to compute a ClickHouse state in T-SQL. One code path, and the
# aggregation semantics are the database's own.

CH_DEFAULT = {"String": "", "UUID": "00000000-0000-0000-0000-000000000000",
              "Date": "1900-01-01", "DateTime64(3)": "1900-01-01 00:00:00.000"}


def _agg_expr(c: ColSpec) -> str:
    out = f"`{c.col}_{c.agg}`"
    if c.agg == "count":
        return f"count() AS {out}"
    if c.agg == "sum":  # Decimal(32,16) overflows on sum; Float64 is what the column is
        return f"sum(toFloat64(`{c.col}`)) AS {out}"
    if c.agg in SIMPLE_AGGS:
        return f"{c.agg}(`{c.col}`) AS {out}"
    return f"{c.agg}State(`{c.col}`) AS {out}"  # avg/uniq/uniqExact are states


def load_queries(spec: TableSpec, types: dict[str, str]) -> tuple[list[str], str, str]:
    """(source columns, the MSSQL SELECT, the ClickHouse INSERT)."""
    raw = list(dict.fromkeys(c.col for c in spec.cols))
    src = f"SELECT {', '.join(raw)} FROM {spec.source}"

    # The grain is every non-aggregate column: they are plain columns in the target,
    # so a GROUP BY that did not include them would have to invent a value for them.
    grain = list(dict.fromkeys(c.col for c in spec.cols if c.role != "aggregate"))
    aggs = [c for c in spec.cols if c.role == "aggregate"]
    if not aggs:
        return raw, src, f"INSERT INTO ax.{spec.name} FORMAT JSONEachRow"

    decl = ", ".join(f"`{c}` {types[c]}" for c in raw)
    select = ", ".join([f"`{c}`" for c in grain] + [_agg_expr(c) for c in aggs])
    group = f" GROUP BY {', '.join(f'`{c}`' for c in grain)}" if grain else ""
    return raw, src, (f"INSERT INTO ax.{spec.name} SELECT {select} "
                      f"FROM input('{decl}'){group} FORMAT JSONEachRow")


@app.post("/api/load")
def load(spec: TableSpec):
    """Truncate the target and reload it from MSSQL. Truncate, not append: this table
    merges rows by key, so appending the same source twice doubles every sum."""
    build_ddl(spec)  # same validation the DDL went through — nothing here is trusted
    types = {c["name"]: c["type"] for c in source_schema()[spec.source]}
    raw, src_sql, insert_sql = load_queries(spec, types)
    # Not Nullable columns, and AX writes blanks rather than NULLs anyway — but a NULL
    # that slips through would abort the whole insert, so it lands as the type's empty.
    blanks = {c: CH_DEFAULT.get(types[c], 0) for c in raw}

    def ch(body: bytes, query: str):
        r = client.post(_ch_base_url(), params={**_ch_params(), "query": query},
                        content=body, timeout=600.0)
        if r.status_code != 200:
            raise HTTPException(502, r.text[:500])

    try:
        ch(b"", f"TRUNCATE TABLE IF EXISTS ax.{spec.name}")
        conn = _mssql_connect()
        cur = conn.cursor()
        cur.execute(src_sql)
        total = 0
        while batch := cur.fetchmany(50_000):  # SALESLINE is 186k rows — one POST is 70MB
            body = b"\n".join(
                json.dumps({c: (blanks[c] if v is None else v)
                            for c, v in zip(raw, row)}, default=str).encode()
                for row in batch)
            ch(body, insert_sql)
            total += len(batch)
        conn.close()
    except HTTPException:
        raise  # ClickHouse already said what was wrong; don't bury it
    except httpx.ConnectError:
        raise HTTPException(502, f"Cannot reach ClickHouse at {_ch_base_url()}")
    except Exception as e:
        raise HTTPException(502, f"Load failed: {e}")
    return {"rows": total, "sql": src_sql}


@app.get("/builder")
def builder():
    return FileResponse(Path(__file__).with_name("builder.html"))


@app.get("/")
def index():
    return FileResponse(Path(__file__).with_name("index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8010)
