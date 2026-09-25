#!/usr/bin/env python3
"""PUC Analytics -- the web app: role-based dashboards over ClickHouse, and a
Run button that loads a billing export into ClickHouse.

    pip install fastapi uvicorn python-multipart httpx openpyxl
    python3 webapp/server.py                    # http://<server>:8020

ClickHouse connection comes from .env (CH_URL, CH_USER, CH_PASSWORD), the same
as the loaders. Users live in ClickHouse database puc_app (APP_DB). The first start
creates one user per role (webapp/roles.py SEED_USERS) with the password in
APP_DEFAULT_PASSWORD (default "Puc@2026") -- change them all on first login.

ponytail: no framework on the front end and no chart library -- plain HTML,
CSS and SVG (webapp/static), so the app works on a PUC network without
internet access and there is nothing to build.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path[:0] = [str(REPO), str(HERE)]

import uvicorn  # noqa: E402
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import ax_load  # noqa: E402  (.env, ch())
import dashboards  # noqa: E402
import review  # noqa: E402
import bulk  # noqa: E402
import formula  # noqa: E402
from roles import ISLANDS, ROLES, SEED_USERS  # noqa: E402

ch = ax_load.ch
APP_DB = os.environ.get("APP_DB", "puc_app")  # users, audit, load timings
UPLOADS = REPO / "data" / "uploads"
SESSION_HOURS = 10
COOKIE = "puc_session"


# =============================================================================
# Users and sessions
# =============================================================================

def secret() -> bytes:
    if os.environ.get("APP_SECRET"):
        return os.environ["APP_SECRET"].encode()
    f = HERE / ".secret"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip().encode()


SECRET = None


def hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def q(sql: str) -> list[dict]:
    return json.loads(ch(sql + " FORMAT JSON"))["data"]


def esc(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


NEW_ROLES = {"reviewer"}


def init_store() -> None:
    ch(f"CREATE DATABASE IF NOT EXISTS {APP_DB}")
    ch(f"""CREATE TABLE IF NOT EXISTS {APP_DB}.users (
            username String, full_name String, role LowCardinality(String), region_code UInt8,
            salt String, pw_hash String, must_change UInt8, active UInt8,
            updated_at DateTime64(3) DEFAULT now64(3))
          ENGINE = ReplacingMergeTree(updated_at) ORDER BY username""")
    ch(f"""CREATE TABLE IF NOT EXISTS {APP_DB}.load_timings (
            ts DateTime DEFAULT now(), kind LowCardinality(String), bytes UInt64, seconds Float32)
          ENGINE = MergeTree ORDER BY ts""")
    ch(f"""CREATE TABLE IF NOT EXISTS {APP_DB}.audit (
            ts DateTime DEFAULT now(), username String, action LowCardinality(String), detail String)
          ENGINE = MergeTree ORDER BY ts""")
    if q(f"SELECT count() AS n FROM {APP_DB}.users")[0]["n"] in (0, "0"):
        pw = os.environ.get("APP_DEFAULT_PASSWORD", "Puc@2026")
        for username, name, role, region in SEED_USERS:
            save_user(username, name, role, region, password=pw, must_change=1)
        print(f"created {len(SEED_USERS)} users (one per role), password '{pw}' -- change them")
    # roles added after the first start get their default user once, if nobody has the role yet
    for username, name, role, region in SEED_USERS:
        if role in NEW_ROLES and q(f"SELECT count() AS n FROM {APP_DB}.users FINAL WHERE role = {esc(role)}")[0]["n"] in (0, "0") \
                and not get_user(username):
            pw = os.environ.get("APP_DEFAULT_PASSWORD", "Puc@2026")
            save_user(username, name, role, region, password=pw, must_change=1)
            print(f"created user '{username}' for the new {role} role, password '{pw}' -- change it")


def get_user(username: str) -> dict | None:
    rows = q(f"SELECT * FROM {APP_DB}.users FINAL WHERE username = {esc(username)}")
    return rows[0] if rows else None


def save_user(username, full_name, role, region, *, password=None, must_change=0, active=1, keep=None):
    salt, pw_hash = (keep["salt"], keep["pw_hash"]) if keep and password is None else (secrets.token_hex(8), None)
    if password is not None:
        pw_hash = hash_pw(password, salt)
    ch(f"INSERT INTO {APP_DB}.users (username, full_name, role, region_code, salt, pw_hash, must_change, active) "
       f"VALUES ({esc(username)}, {esc(full_name)}, {esc(role)}, {int(region)}, {esc(salt)}, {esc(pw_hash)}, "
       f"{int(must_change)}, {int(active)})")


def audit(username: str, action: str, detail: str = "") -> None:
    try:
        ch(f"INSERT INTO {APP_DB}.audit (username, action, detail) VALUES ({esc(username)}, {esc(action)}, {esc(detail)})")
    except Exception:
        pass


def make_token(username: str) -> str:
    exp = int(time.time()) + SESSION_HOURS * 3600
    body = f"{username}|{exp}"
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode()


def current_user(request: Request) -> dict:
    tok = request.cookies.get(COOKIE)
    try:
        username, exp, sig = base64.urlsafe_b64decode(tok.encode()).decode().rsplit("|", 2)
        good = hmac.compare_digest(sig, hmac.new(SECRET, f"{username}|{exp}".encode(), hashlib.sha256).hexdigest())
        if not good or int(exp) < time.time():
            raise ValueError
    except Exception:
        raise HTTPException(401, "Please sign in")
    user = get_user(username)
    if not user or str(user["active"]) != "1" or user["role"] not in ROLES:
        raise HTTPException(401, "Account disabled")
    return user


def scope_of(user: dict) -> dict:
    role = ROLES[user["role"]]
    return {"utilities": list(role.utilities or (1, 2, 3)), "region": int(user["region_code"]) or None,
            "see_accounts": role.see_accounts}


def need(user: dict, page: str) -> None:
    if page not in ROLES[user["role"]].pages:
        raise HTTPException(403, "Your role does not include this page")


# =============================================================================
# App
# =============================================================================

app = FastAPI(title="PUC Analytics", docs_url=None, redoc_url=None)


@app.on_event("startup")
def startup() -> None:
    global SECRET
    SECRET = secret()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    init_store()
    try:  # the raw store and its views; moves an older install's loaded lines onto raw rows, once
        import ub_load
        ub_load.ensure_schema()
    except (Exception, SystemExit) as e:
        print(f"billing schema not ready: {e}", flush=True)


@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    user = get_user(str(body.get("username", "")).strip().lower())
    time.sleep(0.2)  # blunt password guessing a little
    if not user or str(user["active"]) != "1" or hash_pw(str(body.get("password", "")), user["salt"]) != user["pw_hash"]:
        audit(str(body.get("username", "")), "login_failed")
        raise HTTPException(401, "Wrong username or password")
    response.set_cookie(COOKIE, make_token(user["username"]), httponly=True, samesite="lax",
                        max_age=SESSION_HOURS * 3600)
    audit(user["username"], "login")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    user = current_user(request)
    role = ROLES[user["role"]]
    scope = scope_of(user)
    names = {1: "Electricity", 2: "Sewerage", 3: "Water"}
    pages = [{"id": p, "title": dashboards.PAGES[p]["title"], "icon": dashboards.PAGES[p]["icon"],
              "utility": dashboards.PAGES[p]["utility"]}
             for p in role.pages if p in dashboards.PAGES and
             (dashboards.PAGES[p]["utility"] is None or dashboards.PAGES[p]["utility"] in scope["utilities"])]
    return {
        "username": user["username"], "full_name": user["full_name"], "role": user["role"],
        "role_title": role.title, "role_description": role.description,
        "island": ISLANDS[int(user["region_code"])], "must_change": str(user["must_change"]) == "1",
        "pages": pages, "can_load": role.can_load or "load_history" in role.pages, "can_run": role.can_load,
        "can_admin": role.can_admin, "can_review": role.can_review,
        "filters": {
            "periods": dashboards.periods(),
            "regions": [{"code": c, "name": n} for c, n in ISLANDS.items() if c and (not scope["region"] or c == scope["region"])],
            "region_locked": bool(scope["region"]),
            "utilities": [{"code": u, "name": names[u]} for u in scope["utilities"]],
            "sectors": list(dashboards.SECTORS),
            "tariffs": dashboards.tariffs(scope),
        },
    }


@app.get("/api/page/{page_id}")
def page(page_id: str, request: Request, period: str = "", region: int = 0, utility: int = 0,
         sector: str = "", tariff: str = "", compare: str = "", pfrom: str = "", pto: str = ""):
    user = current_user(request)
    if page_id not in dashboards.PAGES:
        raise HTTPException(404, "No such page")
    need(user, page_id)
    scope = scope_of(user)
    valid_periods = {p["period"] for p in dashboards.periods()}
    sel = {"period": period if period in valid_periods else None,
           "region": region if region in (1, 2, 3) else None,
           "utility": utility if utility in scope["utilities"] else None,
           "sector": sector if sector in dashboards.SECTORS else None,
           "tariff": tariff if tariff and tariff in {t["name"] for t in dashboards.tariffs(scope)} else None,
           "compare": compare if compare == "none" or compare in valid_periods else None,
           "range": None}
    if not sel["period"] and pfrom in valid_periods and pto in valid_periods:  # from-to, in either order
        a, b = sorted([pfrom, pto])
        if a == b:
            sel["period"] = a
        else:
            sel["range"] = (a, b)
    pu = dashboards.PAGES[page_id]["utility"]
    if pu and pu not in scope["utilities"]:
        raise HTTPException(403, "Your role does not include this utility")
    return dashboards.page_data(page_id, scope, sel)


# --- data loading ---------------------------------------------------------------

JOBS: dict[str, dict] = {}
JOB_LOCK = threading.Lock()


# First guesses until this server has timed a few loads of its own.
DEFAULT_SECONDS_PER_MB, DEFAULT_REBUILD_SECONDS = 3.0, 8.0


def estimate(kind: str, size: int) -> float:
    """Seconds a job should take, from the last 10 jobs of its kind on this server."""
    try:
        rows = q(f"SELECT bytes, seconds FROM {APP_DB}.load_timings WHERE kind = {esc(kind)} "
                 f"ORDER BY ts DESC LIMIT 10")
    except Exception:
        rows = []
    if kind in ("rebuild", "apply"):
        secs = sorted(float(r["seconds"]) for r in rows)
        return secs[len(secs) // 2] if secs else DEFAULT_REBUILD_SECONDS
    rates = sorted(float(r["seconds"]) / max(int(r["bytes"]), 1) for r in rows)
    rate = rates[len(rates) // 2] if rates else DEFAULT_SECONDS_PER_MB / 1e6
    return max(5.0, rate * size)


def run_job(job_id: str, args: list[str]) -> None:
    job = JOBS[job_id]
    try:
        p = subprocess.Popen([sys.executable, "-u", str(REPO / "ub_load.py"), *args], cwd=REPO,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            job["log"].append(line.rstrip())
        job["status"] = "done" if p.wait() == 0 else "failed"
    except Exception as e:
        job["log"].append(str(e))
        job["status"] = "failed"
    finally:
        job["finished"] = datetime.now().isoformat(timespec="seconds")
        job["elapsed"] = round(time.time() - job["t0"], 1)
        if job["status"] == "done":
            try:
                ch(f"INSERT INTO {APP_DB}.load_timings (kind, bytes, seconds) VALUES "
                   f"({esc(job['kind'])}, {int(job['bytes'])}, {job['elapsed']})")
            except Exception:
                pass
        audit(job["user"], "load_" + job["status"], job["file"])
        JOB_LOCK.release()


def start_job(user: dict, label: str, args: list[str], kind: str = "load", size: int = 0) -> dict:
    if not JOB_LOCK.acquire(blocking=False):
        raise HTTPException(409, "A load is already running -- wait for it to finish")
    job_id = secrets.token_hex(6)
    JOBS[job_id] = {"id": job_id, "status": "running", "file": label, "user": user["username"],
                    "started": datetime.now().isoformat(timespec="seconds"), "finished": None, "log": [],
                    "kind": kind, "bytes": size, "t0": time.time(), "estimate": round(estimate(kind, size), 1)}
    threading.Thread(target=run_job, args=(job_id, args), daemon=True).start()
    audit(user["username"], "load_started", label)
    return {"job": job_id}


@app.post("/api/load")
async def load(request: Request, file: UploadFile = File(...)):
    user = current_user(request)
    if not ROLES[user["role"]].can_load:
        raise HTTPException(403, "Your role cannot load data")
    name = Path(file.filename or "upload").name
    if not name.lower().endswith((".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".zip")):
        raise HTTPException(400, "Upload a .csv, .tsv, .txt, .xlsx or .zip file")
    dest = UPLOADS / f"{datetime.now():%Y%m%d-%H%M%S}_{name}"
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    return start_job(user, name, [str(dest)], "load", dest.stat().st_size)


@app.post("/api/rebuild")
def rebuild(request: Request):
    user = current_user(request)
    if not ROLES[user["role"]].can_load:
        raise HTTPException(403, "Your role cannot run the pipeline")
    return start_job(user, "Rebuild aggregates", ["--rebuild"], "rebuild")


@app.get("/api/load/{job_id}")
def job(job_id: str, request: Request):
    current_user(request)
    if job_id not in JOBS:
        raise HTTPException(404, "No such job")
    job = JOBS[job_id]
    if job["status"] == "running":
        job["elapsed"] = round(time.time() - job["t0"], 1)
    return job


# --- data review ---------------------------------------------------------------------

def reviewer(request: Request) -> dict:
    user = current_user(request)
    if not ROLES[user["role"]].can_review:
        raise HTTPException(403, "Your role cannot review data")
    return user


def review_call(fn, *args, locked=True):
    """Edits wait while a load or a finalize is running, so none is lost to the swap."""
    if locked and JOB_LOCK.locked():
        raise HTTPException(409, "A load or finalize is running -- try again when it has finished")
    try:
        return fn(*args)
    except review.Invalid as e:
        raise HTTPException(400, str(e))
    except formula.FormulaError as e:
        return JSONResponse({"detail": str(e), "pos": e.pos, "field": getattr(e, "field", None)}, 400)


@app.get("/api/review/columns")
def review_columns(request: Request):
    reviewer(request)
    return review.columns()


@app.get("/api/review/batches")
def review_batches(request: Request):
    reviewer(request)
    return {"batches": review.batches(), "running": next((j for j in JOBS.values() if j["status"] == "running"), None)}


@app.get("/api/review/{batch_id}/rows")
def review_rows(batch_id: str, request: Request, search: str = "", changed: int = 0, page: int = 1,
                size: int = 50, utility: int = 0, region: int = 0, rule: str = ""):
    reviewer(request)
    try:
        review.batch(batch_id)
    except review.Invalid as e:
        raise HTTPException(404, str(e))
    try:
        parsed = json.loads(rule) if rule else None
    except ValueError:
        raise HTTPException(400, "Malformed rule")
    return review_call(review.rows, batch_id, search[:100], bool(changed), page, size, utility, region, parsed,
                       locked=False)


# --- formula builder: a rule picks rows, an action changes them (webapp/bulk.py) ---

@app.get("/api/review/formula/reference")
def formula_reference(request: Request):
    reviewer(request)
    return bulk.reference()


@app.post("/api/review/{batch_id}/formula/preview")
async def formula_preview(batch_id: str, request: Request):
    reviewer(request)
    return review_call(bulk.preview, batch_id, await request.json(), locked=False)


@app.post("/api/review/{batch_id}/formula/apply")
async def formula_apply(batch_id: str, request: Request):
    user = reviewer(request)
    spec = await request.json()
    audit(user["username"], "formula", f"{batch_id}: {json.dumps(spec)[:900]}")
    return review_call(bulk.apply, batch_id, spec, user["username"])


@app.get("/api/review/formulas/saved")
def formulas_saved(request: Request):
    reviewer(request)
    return {"saved": bulk.saved()}


@app.post("/api/review/formulas/save")
async def formulas_save(request: Request):
    user = reviewer(request)
    b = await request.json()
    return review_call(bulk.save, b.get("name"), b.get("spec"), user["username"], locked=False)


@app.post("/api/review/formulas/forget")
async def formulas_forget(request: Request):
    user = reviewer(request)
    b = await request.json()
    return review_call(bulk.forget, str(b.get("name", "")), user["username"], locked=False)


@app.get("/api/review/{batch_id}/history")
def review_history(batch_id: str, request: Request):
    reviewer(request)
    return {"history": review.history(batch_id)}


@app.post("/api/review/{batch_id}/edit")
async def review_edit(batch_id: str, request: Request):
    user = reviewer(request)
    b = await request.json()
    if not isinstance(b.get("changes"), dict) or not b["changes"]:
        raise HTTPException(400, "Nothing to change")
    return review_call(review.edit, batch_id, int(b.get("line_no", 0)), b["changes"], user["username"])


@app.post("/api/review/{batch_id}/add")
async def review_add(batch_id: str, request: Request):
    user = reviewer(request)
    b = await request.json()
    return review_call(review.add, batch_id, b.get("values") or {}, user["username"])


@app.post("/api/review/{batch_id}/delete")
async def review_delete(batch_id: str, request: Request):
    user = reviewer(request)
    b = await request.json()
    return review_call(review.delete, batch_id, int(b.get("line_no", 0)), user["username"])


@app.post("/api/review/{batch_id}/undo")
async def review_undo(batch_id: str, request: Request):
    user = reviewer(request)
    b = await request.json()
    return review_call(review.undo, batch_id, int(b.get("line_no", 0)), user["username"])


@app.post("/api/review/{batch_id}/discard")
def review_discard(batch_id: str, request: Request):
    user = reviewer(request)
    return review_call(review.discard, batch_id, user["username"])


@app.post("/api/review/{batch_id}/finalize")
def review_finalize(batch_id: str, request: Request):
    """Write the pending edits into the raw rows and rebuild the aggregates and dashboards."""
    user = reviewer(request)
    try:
        b = review.batch(batch_id)
    except review.Invalid as e:
        raise HTTPException(404, str(e))
    if not int(b["pending"]):
        raise HTTPException(400, "There are no pending edits to finalize")
    label = f"Finalize {b['period'][:7] or batch_id[:8]}"
    audit(user["username"], "finalize", batch_id)
    return start_job(user, label, ["--apply", batch_id, user["username"]], "apply")


@app.post("/api/review/{batch_id}/reviewed")
def review_reviewed(batch_id: str, request: Request):
    user = reviewer(request)
    audit(user["username"], "reviewed", batch_id)
    return review_call(review.reviewed, batch_id, user["username"])


@app.get("/api/loads")
def loads(request: Request):
    user = current_user(request)
    role = ROLES[user["role"]]
    if not (role.can_load or "load_history" in role.pages):
        raise HTTPException(403, "Your role cannot see loads")
    try:
        batches = q("SELECT b.batch_id AS batch_id, toString(b.period_month) AS period, b.lines AS lines, "
                    "toFloat64(b.amount) AS amount, b.invoices AS invoices, ifNull(s.status, 'draft') AS status, "
                    "ifNull(s.revision, 1) AS revision FROM ub.v_batches AS b "
                    "LEFT JOIN ub.v_batch_status AS s ON s.batch_id = b.batch_id "
                    "ORDER BY b.period_month SETTINGS join_use_nulls = 1")
        history = q("SELECT toString(loaded_at) AS loaded_at, file_name, batch_id, rows, toFloat64(amount) AS amount "
                    "FROM ub.load_log ORDER BY loaded_at DESC LIMIT 30")
    except Exception:
        batches, history = [], []
    running = next((j for j in JOBS.values() if j["status"] == "running"), None)
    return {"batches": batches, "history": history, "running": running}


# --- users ---------------------------------------------------------------------------

def need_admin(request: Request) -> dict:
    user = current_user(request)
    if not ROLES[user["role"]].can_admin:
        raise HTTPException(403, "Administrators only")
    return user


@app.get("/api/users")
def users(request: Request):
    need_admin(request)
    rows = q("SELECT username, full_name, role, region_code, active, must_change, toString(updated_at) AS updated "
             f"FROM {APP_DB}.users FINAL ORDER BY username")
    return {"users": rows,
            "roles": [{"id": k, "title": r.title, "description": r.description} for k, r in ROLES.items()],
            "islands": [{"code": c, "name": n} for c, n in ISLANDS.items()]}


@app.post("/api/users")
async def upsert_user(request: Request):
    admin = need_admin(request)
    b = await request.json()
    username = str(b.get("username", "")).strip().lower()
    if not username or not all(ch_.isalnum() or ch_ in "._-" for ch_ in username):
        raise HTTPException(400, "Username: letters, digits, dot, dash or underscore")
    if b.get("role") not in ROLES:
        raise HTTPException(400, "Unknown role")
    region = int(b.get("region_code") or 0)
    if region not in ISLANDS:
        raise HTTPException(400, "Unknown island")
    if b.get("role") == "regional_manager" and not region:
        raise HTTPException(400, "A regional manager needs an island")
    existing = get_user(username)
    password = b.get("password") or None
    if not existing and not password:
        raise HTTPException(400, "A new user needs a password")
    if password and len(password) < 8:
        raise HTTPException(400, "Passwords need at least 8 characters")
    if existing and username == admin["username"] and (not b.get("active", True) or b["role"] != "admin"):
        raise HTTPException(400, "You cannot demote or disable your own account")
    save_user(username, str(b.get("full_name") or username), b["role"], region, password=password,
              must_change=1 if password else int(existing["must_change"]) if existing else 1,
              active=1 if b.get("active", True) else 0, keep=existing)
    audit(admin["username"], "user_saved", username)
    return {"ok": True}


@app.post("/api/me/password")
async def change_password(request: Request):
    user = current_user(request)
    b = await request.json()
    if hash_pw(str(b.get("current", "")), user["salt"]) != user["pw_hash"]:
        raise HTTPException(400, "Current password is wrong")
    new = str(b.get("new", ""))
    if len(new) < 8:
        raise HTTPException(400, "Passwords need at least 8 characters")
    save_user(user["username"], user["full_name"], user["role"], user["region_code"], password=new, must_change=0,
              active=1, keep=user)
    audit(user["username"], "password_changed")
    return {"ok": True}


@app.exception_handler(RuntimeError)
def ch_error(request: Request, exc: RuntimeError):
    return JSONResponse({"detail": "Database error: " + str(exc).split("\n")[0][:300]}, status_code=502)


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index():
    # stamp the script and style links with their file times, so after a git pull the browser
    # fetches the new files instead of a cached copy
    html = (HERE / "static" / "index.html").read_text()
    for name in ("app.css", "charts.js", "app.js"):
        v = int((HERE / "static" / name).stat().st_mtime)
        html = html.replace(f'/static/{name}"', f'/static/{name}?v={v}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("APP_HOST", "0.0.0.0"), port=int(os.environ.get("APP_PORT", "8020")))
