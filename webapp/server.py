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
import re
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import ax_load  # noqa: E402  (.env, ch())
import dashboards  # noqa: E402
import review  # noqa: E402
import bulk  # noqa: E402
import formula  # noqa: E402
import auth  # noqa: E402
from roles import ISLANDS, ROLES, SEED_USERS, combine, role_ids  # noqa: E402

ch = ax_load.ch
APP_DB = os.environ.get("APP_DB", "puc_app")  # users, audit, load timings
UPLOADS = REPO / "data" / "uploads"
SESSION_HOURS = 10
COOKIE = "puc_session"


def app_version() -> str:
    """The commit this server runs, so "did the update arrive?" has an answer on the Users page."""
    try:
        out = subprocess.run(["git", "-C", str(REPO), "log", "-1", "--format=%h %cs"], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


VERSION = app_version()
FLOW_COOKIE = "puc_sso"  # the few minutes between "Continue with Google" and coming back


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
    auth.init_store()  # e-mail and Google / Microsoft columns, invitation and reset links
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
        if role in NEW_ROLES and q(f"SELECT count() AS n FROM {APP_DB}.users FINAL "
                                   f"WHERE has(splitByChar(',', role), {esc(role)})")[0]["n"] in (0, "0") \
                and not get_user(username):
            pw = os.environ.get("APP_DEFAULT_PASSWORD", "Puc@2026")
            save_user(username, name, role, region, password=pw, must_change=1)
            print(f"created user '{username}' for the new {role} role, password '{pw}' -- change it")


def get_user(username: str) -> dict | None:
    rows = q(f"SELECT * FROM {APP_DB}.users FINAL WHERE username = {esc(username)}")
    return rows[0] if rows else None


def find_user(field: str, value: str) -> dict | None:
    """The user whose email / google_sub / microsoft_sub is this (never matches empty)."""
    assert field in ("email", "google_sub", "microsoft_sub")
    if not value:
        return None
    rows = q(f"SELECT * FROM {APP_DB}.users FINAL WHERE {field} = {esc(value)} ORDER BY username LIMIT 1")
    return rows[0] if rows else None


LINKS = ("email", "google_sub", "microsoft_sub", "created_by")


def save_user(username, full_name, role, region, *, password=None, must_change=0, active=1, keep=None, **links):
    """One row per user; what is not given is kept from `keep` (the current row). A user
    made without a password (Google / Microsoft only) has an empty hash, which no password matches."""
    salt, pw_hash = (keep["salt"], keep["pw_hash"]) if keep and password is None else (secrets.token_hex(8), "")
    if password is not None:
        pw_hash = hash_pw(password, salt)
    vals = {k: links[k] if links.get(k) is not None else (keep or {}).get(k, "") for k in LINKS}
    ch(f"INSERT INTO {APP_DB}.users (username, full_name, role, region_code, salt, pw_hash, must_change, active, "
       f"{', '.join(LINKS)}) VALUES ({esc(username)}, {esc(full_name)}, {esc(role)}, {int(region)}, {esc(salt)}, "
       f"{esc(pw_hash)}, {int(must_change)}, {int(active)}, {', '.join(esc(vals[k]) for k in LINKS)})")


PASSWORD_RULE = ("Passwords need at least 8 characters, with at least one letter, one number and one special "
                 "character (like ! @ # $ % & * -)")


def password_problem(password: str) -> str:
    """"" when a new password is strong enough, else the rule. Checked wherever a password is set;
    existing passwords keep working until they are changed."""
    if (len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password)
            or not re.search(r"[^A-Za-z0-9]", password)):
        return PASSWORD_RULE
    return ""


def password_ok(user: dict, password: str) -> bool:
    return bool(user["pw_hash"]) and hmac.compare_digest(hash_pw(password, user["salt"]), user["pw_hash"])


def audit(username: str, action: str, detail: str = "") -> None:
    try:
        ch(f"INSERT INTO {APP_DB}.audit (username, action, detail) VALUES ({esc(username)}, {esc(action)}, {esc(detail)})")
    except Exception:
        pass


def pw_mark(user: dict) -> str:
    """Changes whenever the password does: sessions made before a password change stop working."""
    return hashlib.sha256((user["salt"] + user["pw_hash"]).encode()).hexdigest()[:12]


def make_token(user: dict) -> str:
    exp = int(time.time()) + SESSION_HOURS * 3600
    body = f"{user['username']}|{pw_mark(user)}|{exp}"
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode()


def sign_in(response: Response, user: dict, how: str) -> None:
    response.set_cookie(COOKIE, make_token(user), httponly=True, samesite="lax", max_age=SESSION_HOURS * 3600,
                        secure=auth.base_url().startswith("https://"))
    audit(user["username"], "login", how)


def current_user(request: Request) -> dict:
    tok = request.cookies.get(COOKIE)
    try:
        username, mark, exp, sig = base64.urlsafe_b64decode(tok.encode()).decode().rsplit("|", 3)
        good = hmac.compare_digest(sig, hmac.new(SECRET, f"{username}|{mark}|{exp}".encode(),
                                                 hashlib.sha256).hexdigest())
        if not good or int(exp) < time.time():
            raise ValueError
    except Exception:
        raise HTTPException(401, "Please sign in")
    user = get_user(username)
    if not user or str(user["active"]) != "1" or not valid_roles(user["role"]):
        raise HTTPException(401, "Account disabled")
    if not hmac.compare_digest(mark, pw_mark(user)):
        raise HTTPException(401, "Your password was changed. Please sign in again.")
    return user


def valid_roles(value) -> bool:
    ids = role_ids(value)
    return bool(ids) and all(r in ROLES for r in ids)


def urole(user: dict):
    """The user's access: their one role, or all of their roles together."""
    return combine(user["role"])


def scope_of(user: dict) -> dict:
    role = urole(user)
    return {"utilities": list(role.utilities or (1, 2, 3)), "region": int(user["region_code"]) or None,
            "see_accounts": role.see_accounts}


def need(user: dict, page: str) -> None:
    if page not in urole(user).pages:
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
    name = str(body.get("username", "")).strip().lower()[:254]  # a username or an e-mail address
    user = find_user("email", name) if "@" in name else get_user(name)
    key = user["username"] if user else name
    wait = auth.locked_for(key)
    if wait:
        audit(key, "login_locked")
        raise HTTPException(429, f"Too many wrong passwords. Try again in {(wait + 59) // 60} minutes, "
                                 "or use \"Forgot password\".")
    time.sleep(0.2)  # blunt password guessing a little
    if not user or str(user["active"]) != "1" or not password_ok(user, str(body.get("password", ""))):
        auth.failed(key)
        audit(key, "login_failed")
        raise HTTPException(401, "Wrong username or password")
    auth.clear_fails(key)
    sign_in(response, user, "password")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


# --- sign-in options, invitations, password reset, Google / Microsoft ---------------

@app.get("/api/auth/config")
def auth_config():
    """What the sign-in page may offer (no session needed)."""
    return {"sso": [{"id": k, "title": p["title"]} for k, p in auth.sso_ready().items()],
            "forgot": auth.mail_configured() and bool(auth.base_url())}


def link_base(request: Request) -> str:
    # an administrator's own request may supply the address when APP_BASE_URL is not set;
    # password resets (asked for by anyone) never do -- see auth_forgot
    return auth.base_url() or str(request.base_url).rstrip("/")


def invite_public(row: dict) -> dict:
    return {"email": row["email"], "full_name": row["full_name"], "role": row["role"],
            "roles": [{"id": r, "title": ROLES[r].title} for r in role_ids(row["role"]) if r in ROLES],
            "role_title": combine(row["role"]).title if valid_roles(row["role"]) else row["role"],
            "island": ISLANDS.get(row["region_code"], ""), "expires_at": row["expires_at"],
            "needs_code": auth.mail_configured(),
            "sso": [{"id": k, "title": p["title"]} for k, p in auth.sso_ready().items()]}


def send_invite(row: dict, token: str, admin: dict, request: Request) -> dict:
    """E-mails the link when e-mail is set up; the administrator always gets the link too."""
    link = f"{link_base(request)}/#/invite/{token}"
    sent, error = False, ""
    if auth.mail_configured():
        try:
            auth.send_mail(row["email"], *auth.invite_mail(row["email"], row["full_name"], combine(row["role"]).title,
                                                           ISLANDS.get(row["region_code"], ""), admin["full_name"], link))
            sent = True
        except (RuntimeError, ValueError) as e:
            error = str(e)
    else:
        error = "E-mail is not set up, so copy the link and send it yourself."
    if sent:
        auth.put_token({**row, "sent": 1})
    return {"ok": True, "link": link, "sent": sent, "error": error, "email": row["email"]}


def check_roles(b: dict) -> tuple[str, int]:
    """The roles ("roles": [...], or one "role") and island from a form, checked; roles as stored."""
    ids = role_ids(b.get("roles") if b.get("roles") is not None else b.get("role"))
    if not ids:
        raise HTTPException(400, "Choose at least one role")
    if not all(r in ROLES for r in ids):
        raise HTTPException(400, "Unknown role")
    region = int(b.get("region_code") or 0)
    if region not in ISLANDS:
        raise HTTPException(400, "Unknown island")
    if "regional_manager" in ids and not region:
        raise HTTPException(400, "A regional manager needs an island")
    return ",".join(ids), region


def check_new_person(email: str, b: dict) -> tuple[str, int]:
    if not auth.valid_email(email):
        raise HTTPException(400, "Enter a valid e-mail address")
    return check_roles(b)


@app.get("/api/invites")
def invites_list(request: Request):
    need_admin(request)
    return {"invites": auth.invites()}


@app.post("/api/invites")
async def invite_create(request: Request):
    admin = need_admin(request)
    b = await request.json()
    email = str(b.get("email", "")).strip().lower()
    role, region = check_new_person(email, b)
    if find_user("email", email):
        raise HTTPException(400, "A user with this e-mail address already exists")
    for old in auth.invites():  # one open invitation per address: a new one replaces it
        if old["email"] == email and old["status"] == "pending":
            auth.close_token(auth.invite_by_id(old["id"]), "revoked", admin["username"])
    token, row = auth.new_token("invite", email=email, full_name=str(b.get("full_name") or "").strip()[:100],
                                role=role, region=region, by=admin["username"], minutes=auth.INVITE_DAYS * 1440)
    audit(admin["username"], "invite_sent", f"{email} as {role}")
    return send_invite(row, token, admin, request)


@app.post("/api/invites/{invite_id}/resend")
def invite_resend(invite_id: str, request: Request):
    """A new link (the old one stops working) and a fresh expiry."""
    admin = need_admin(request)
    row = auth.invite_by_id(invite_id)
    if not row or row["status"] not in ("pending", "expired"):
        raise HTTPException(400, "Only open or expired invitations can be sent again")
    if find_user("email", row["email"]):
        raise HTTPException(400, "A user with this e-mail address already exists")
    token, row = auth.new_token("invite", email=row["email"], full_name=row["full_name"], role=row["role"],
                                region=row["region_code"], by=admin["username"], minutes=auth.INVITE_DAYS * 1440,
                                id_=row["id"], created_at=row["created_at"])
    audit(admin["username"], "invite_resent", row["email"])
    return send_invite(row, token, admin, request)


@app.post("/api/invites/{invite_id}/revoke")
def invite_revoke(invite_id: str, request: Request):
    admin = need_admin(request)
    row = auth.invite_by_id(invite_id)
    if not row or row["status"] != "pending":
        raise HTTPException(400, "Only open invitations can be withdrawn")
    auth.close_token(row, "revoked", admin["username"])
    audit(admin["username"], "invite_revoked", row["email"])
    return {"ok": True}


@app.get("/api/invite/{token}")
def invite_get(token: str):
    row = auth.find_token("invite", token)
    if not row:
        raise HTTPException(404, "This invitation link is not valid any more. It may have been used, withdrawn "
                                 "or expired -- ask your administrator for a new one.")
    return invite_public(row)


@app.post("/api/invite/{token}/code")
def invite_code(token: str):
    """E-mails a 6-digit code to the invited address. Only whoever reads that inbox can finish,
    so a forwarded invitation does not work for anyone else."""
    row = auth.find_token("invite", token)
    if not row:
        raise HTTPException(404, "This invitation link is not valid any more")
    if not auth.mail_configured():
        raise HTTPException(400, "E-mail is not set up, so no code is needed")
    wait = auth.code_send_wait(row["id"])
    if wait:
        raise HTTPException(429, f"A code was sent just now. You can send another in {wait} seconds.")
    code = auth.new_code(row)
    try:
        auth.send_mail(row["email"], *auth.code_mail(row["full_name"], code))
    except (RuntimeError, ValueError):
        raise HTTPException(502, "The code could not be e-mailed. Try again in a minute.")
    audit(row["email"], "invite_code_sent")
    return {"ok": True, "email": row["email"], "minutes": auth.CODE_MINUTES}


def free_username(email: str) -> str:
    base = "".join(c for c in email.split("@")[0].lower() if c.isalnum() or c in "._-")[:30] or "user"
    name, n = base, 1
    while get_user(name):
        n += 1
        name = f"{base}{n}"
    return name


def accept_invite(row: dict, username: str, full_name: str, *, password=None, **links) -> dict:
    """Makes the invited account. Caller holds auth.ACCEPT_LOCK and has re-read the row."""
    if find_user("email", row["email"]):
        raise HTTPException(400, "An account with this e-mail address already exists. Sign in instead.")
    save_user(username, full_name, row["role"], row["region_code"], password=password, must_change=0, active=1,
              email=row["email"], created_by=row["created_by"], **links)
    auth.close_token(row, "accepted", username)
    audit(username, "invite_accepted", f"{row['email']} invited by {row['created_by']}")
    return get_user(username)


@app.post("/api/invite/{token}/accept")
async def invite_accept(token: str, request: Request, response: Response):
    b = await request.json()
    username = str(b.get("username", "")).strip().lower()
    full_name = str(b.get("full_name", "")).strip()[:100]
    password = str(b.get("password", ""))
    if not username or len(username) > 40 or not all(c.isalnum() or c in "._-" for c in username):
        raise HTTPException(400, "Username: letters, digits, dot, dash or underscore (up to 40)")
    if not full_name:
        raise HTTPException(400, "Enter your full name")
    if password_problem(password):
        raise HTTPException(400, PASSWORD_RULE)
    with auth.ACCEPT_LOCK:
        row = auth.find_token("invite", token)
        if not row:
            raise HTTPException(404, "This invitation link is not valid any more")
        if auth.mail_configured():  # the code sent to the invited address, see invite_code
            problem = auth.check_code(row, b.get("code", ""))
            if problem:
                audit(row["email"], "invite_code_wrong")
                raise HTTPException(400, problem)
        if get_user(username):
            raise HTTPException(400, "This username is taken. Choose another one.")
        user = accept_invite(row, username, full_name, password=password)
    sign_in(response, user, "invitation")
    return {"ok": True, "username": username}


@app.post("/api/auth/forgot")
async def auth_forgot(request: Request):
    """Always the same answer, so the form does not tell who has an account."""
    b = await request.json()
    name = str(b.get("username", "")).strip().lower()[:254]
    answer = {"ok": True, "message": "If that account has an e-mail address, a reset link is on its way. "
                                      f"It works for {auth.RESET_MINUTES} minutes."}
    if not (auth.mail_configured() and auth.base_url()):
        raise HTTPException(400, "Password reset by e-mail is not set up. Ask your administrator.")
    user = find_user("email", name) if "@" in name else get_user(name)
    if not user or not user["email"] or str(user["active"]) != "1" or not auth.reset_allowed(user["username"]):
        time.sleep(0.3)
        return answer
    token, _ = auth.new_token("reset", email=user["email"], username=user["username"], by=user["username"],
                              minutes=auth.RESET_MINUTES)
    try:
        auth.send_mail(user["email"], *auth.reset_mail(user["full_name"], user["username"],
                                                       f"{auth.base_url()}/#/reset/{token}"))
        audit(user["username"], "reset_requested")
    except (RuntimeError, ValueError) as e:
        audit(user["username"], "reset_mail_failed", str(e)[:200])
    return answer


@app.get("/api/auth/reset/{token}")
def auth_reset_get(token: str):
    row = auth.find_token("reset", token)
    if not row:
        raise HTTPException(404, "This reset link is not valid any more. Ask for a new one.")
    return {"username": row["username"]}


@app.post("/api/auth/reset/{token}")
async def auth_reset(token: str, request: Request, response: Response):
    b = await request.json()
    password = str(b.get("password", ""))
    if password_problem(password):
        raise HTTPException(400, PASSWORD_RULE)
    with auth.ACCEPT_LOCK:
        row = auth.find_token("reset", token)
        user = get_user(row["username"]) if row else None
        if not row or not user or str(user["active"]) != "1":
            raise HTTPException(404, "This reset link is not valid any more. Ask for a new one.")
        save_user(user["username"], user["full_name"], user["role"], user["region_code"], password=password,
                  must_change=0, active=1, keep=user)
        auth.close_token(row, "used", user["username"])
    auth.clear_fails(user["username"])
    audit(user["username"], "password_reset")
    sign_in(response, get_user(user["username"]), "password reset")
    return {"ok": True}


def back_to_app(error: str = "") -> RedirectResponse:
    r = RedirectResponse(("/?signin_error=" + error.replace(" ", "+")[:300] + "#/") if error else "/#/", 303)
    r.delete_cookie(FLOW_COOKIE, path="/api/auth/oidc")
    return r


@app.get("/api/auth/oidc/{provider}/start")
def oidc_start(provider: str, invite: str = ""):
    if provider not in auth.sso_ready():
        raise HTTPException(404, "This sign-in option is not set up")
    hint = ""
    if invite:
        row = auth.find_token("invite", invite)
        if not row:
            return back_to_app("This invitation link is not valid any more")
        hint = row["email"]
    url, flow = auth.start(provider, SECRET, invite, hint)
    r = RedirectResponse(url, 303)
    r.set_cookie(FLOW_COOKIE, flow, max_age=600, httponly=True, samesite="lax", path="/api/auth/oidc",
                 secure=auth.base_url().startswith("https://"))
    return r


@app.get("/api/auth/oidc/{provider}/callback")
def oidc_callback(provider: str, request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return back_to_app("The sign-in was cancelled")
    flow = auth.unsign(SECRET, request.cookies.get(FLOW_COOKIE))
    try:
        who = auth.finish(provider, flow, code, state)
    except auth.SSOError as e:
        return back_to_app(str(e))
    field, title = f"{provider}_sub", auth.sso_ready()[provider]["title"]
    user = find_user(field, who["sub"])
    if flow.get("i") and not user:  # accepting an invitation
        with auth.ACCEPT_LOCK:
            row = auth.find_token("invite", flow["i"])
            if not row:
                return back_to_app("This invitation link is not valid any more")
            if who["email"] != row["email"]:
                return back_to_app(f"The invitation is for {row['email']} but that {title} account is "
                                   f"{who['email'] or 'without an address'}. Choose the right account, or "
                                   "create a username and password instead.")
            if not who["email_trusted"]:  # the provider must vouch that the address is theirs
                return back_to_app(f"{title} did not confirm that this account owns {row['email']}. "
                                   "Create a username and password instead (a code is e-mailed to you).")
            try:
                user = accept_invite(row, free_username(row["email"]), row["full_name"] or who["name"] or row["email"],
                                     **{field: who["sub"]})
            except HTTPException as e:
                return back_to_app(e.detail)
    elif not user and who["email_trusted"]:
        # first Google / Microsoft sign-in of an existing user whose address the provider vouches for
        user = find_user("email", who["email"])
        if user and not user[field]:
            save_user(user["username"], user["full_name"], user["role"], user["region_code"],
                      must_change=user["must_change"], active=user["active"], keep=user, **{field: who["sub"]})
            audit(user["username"], "sso_linked", provider)
            user = get_user(user["username"])
        elif user:
            user = None  # this address belongs to someone who already uses another account there
    if not user:
        audit(who["email"] or who["sub"], "login_failed", provider)
        return back_to_app(f"No PUC Analytics account uses this {title} account. Ask your administrator "
                           "for an invitation.")
    if str(user["active"]) != "1" or not valid_roles(user["role"]):
        return back_to_app("Your account is disabled")
    r = back_to_app()
    r.set_cookie(COOKIE, make_token(user), httponly=True, samesite="lax", max_age=SESSION_HOURS * 3600,
                 secure=auth.base_url().startswith("https://"))
    audit(user["username"], "login", provider)
    return r


@app.get("/api/me")
def me(request: Request):
    user = current_user(request)
    role = urole(user)
    scope = scope_of(user)
    names = {1: "Electricity", 2: "Sewerage", 3: "Water"}
    pages = [{"id": p, "title": dashboards.PAGES[p]["title"], "icon": dashboards.PAGES[p]["icon"],
              "utility": dashboards.PAGES[p]["utility"]}
             for p in role.pages if p in dashboards.PAGES and
             (dashboards.PAGES[p]["utility"] is None or dashboards.PAGES[p]["utility"] in scope["utilities"])]
    return {
        "username": user["username"], "full_name": user["full_name"], "role": user["role"],
        "roles": role_ids(user["role"]), "role_title": role.title, "role_description": role.description,
        "email": user["email"],
        "has_password": bool(user["pw_hash"]),
        # the code on disk was updated (git pull) but this server still runs the old one
        "restart_needed": role.can_admin and bool(VERSION) and app_version() != VERSION,
        "version": VERSION,
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
    if not urole(user).can_load:
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
    if not urole(user).can_load:
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
    if not urole(user).can_review:
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


@app.post("/api/review/columns/remove")
async def columns_remove(request: Request):
    """Hide an added column everywhere; its values stay in the rows and the change log."""
    user = reviewer(request)
    b = await request.json()
    audit(user["username"], "column_removed", str(b.get("ident", "")))
    return review_call(bulk.remove_column, str(b.get("ident", "")), user["username"], locked=False)


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
    role = urole(user)
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
    if not urole(user).can_admin:
        raise HTTPException(403, "Administrators only")
    return user


@app.get("/api/users")
def users(request: Request):
    need_admin(request)
    rows = q("SELECT username, full_name, email, role, region_code, active, must_change, created_by, "
             "google_sub != '' AS google, microsoft_sub != '' AS microsoft, pw_hash != '' AS has_password, "
             f"toString(updated_at) AS updated FROM {APP_DB}.users FINAL ORDER BY username")
    for r in rows:
        r["locked"] = auth.locked_for(r["username"]) > 0
        r["roles"] = role_ids(r["role"])
    return {"users": rows, "version": VERSION,
            "mail": {"configured": auth.mail_configured(), "base_url": auth.base_url(),
                     "from": os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", ""),
                     "sso": [p["title"] for p in auth.sso_ready().values()],
                     "sso_waiting": [p["title"] for k, p in auth.providers().items() if k not in auth.sso_ready()]},
            "roles": [{"id": k, "title": r.title, "description": r.description} for k, r in ROLES.items()],
            "islands": [{"code": c, "name": n} for c, n in ISLANDS.items()]}


@app.post("/api/users")
async def upsert_user(request: Request):
    admin = need_admin(request)
    b = await request.json()
    username = str(b.get("username", "")).strip().lower()
    if not username or len(username) > 40 or not all(ch_.isalnum() or ch_ in "._-" for ch_ in username):
        raise HTTPException(400, "Username: letters, digits, dot, dash or underscore (up to 40)")
    email = str(b.get("email") or "").strip().lower()
    if email and not auth.valid_email(email):
        raise HTTPException(400, "Enter a valid e-mail address, or leave it empty")
    role, region = check_roles(b)
    existing = get_user(username)
    if b.get("new") and existing:
        raise HTTPException(400, "This username is taken. Choose another one.")
    other = find_user("email", email)
    if other and other["username"] != username:
        raise HTTPException(400, f"This e-mail address belongs to '{other['username']}'")
    password = b.get("password") or None
    if not existing and not password:
        raise HTTPException(400, "A new user needs a password")
    if password and password_problem(password):
        raise HTTPException(400, PASSWORD_RULE)
    if existing and username == admin["username"] and (not b.get("active", True) or "admin" not in role_ids(role)):
        raise HTTPException(400, "You cannot demote or disable your own account")
    save_user(username, str(b.get("full_name") or username), role, region, password=password,
              must_change=1 if password else int(existing["must_change"]) if existing else 1,
              active=1 if b.get("active", True) else 0, keep=existing, email=email,
              created_by=None if existing else admin["username"],
              # changing the address forgets the Google / Microsoft accounts linked through the old one
              **({"google_sub": "", "microsoft_sub": ""} if existing and existing["email"] != email else {}))
    if b.get("unlink"):
        u = get_user(username)
        save_user(username, u["full_name"], u["role"], u["region_code"], must_change=u["must_change"],
                  active=u["active"], keep=u, google_sub="", microsoft_sub="")
    auth.clear_fails(username)  # saving also lifts a wrong-password lock
    audit(admin["username"], "user_saved", username)
    return {"ok": True}


@app.post("/api/me/password")
async def change_password(request: Request, response: Response):
    user = current_user(request)
    b = await request.json()
    # someone who signs in with Google / Microsoft only has no current password to give
    if user["pw_hash"] and not password_ok(user, str(b.get("current", ""))):
        raise HTTPException(400, "Current password is wrong")
    new = str(b.get("new", ""))
    if password_problem(new):
        raise HTTPException(400, PASSWORD_RULE)
    save_user(user["username"], user["full_name"], user["role"], user["region_code"], password=new, must_change=0,
              active=1, keep=user)
    audit(user["username"], "password_changed")
    sign_in(response, get_user(user["username"]), "password changed")  # other sessions end, this one goes on
    return {"ok": True}


@app.post("/api/mail/test")
async def mail_test(request: Request):
    admin = need_admin(request)
    b = await request.json()
    to = str(b.get("to") or admin["email"]).strip().lower()
    link = link_base(request) + "/#/"
    try:
        auth.send_mail(to, "PUC Analytics: test e-mail", f"E-mail from PUC Analytics works.\n\n{link}\n",
                       auth._mail_html("E-mail works", ["This test message from PUC Analytics arrived, so "
                                       "invitations and password resets can be e-mailed."], "Open PUC Analytics",
                                       link, f"Sent by {admin['username']}."))
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    audit(admin["username"], "mail_test", to)
    return {"ok": True, "to": to}


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
