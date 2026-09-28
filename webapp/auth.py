"""Sign-in helpers for the web app: e-mail, one-time links (invitations and
password resets), Google / Microsoft sign-in, and the wrong-password lockout.

Configuration (all optional, in .env -- see .env.example):

    APP_BASE_URL     the address people open, e.g. https://puc-analytics.example.sc
                     Links in e-mails are built from it, never from the request.
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_TLS (starttls|ssl|none)
                     Gmail: smtp.gmail.com 587 with an app password.
                     Microsoft 365: smtp.office365.com 587.
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET          "Continue with Google"
    MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT        "Continue with Microsoft"

Without SMTP an invitation still works: the administrator gets the link to
copy and send. Without the client keys the buttons are simply not shown.

Tokens: 32 random bytes, sent once, stored only as a SHA-256 hash, single use,
with an expiry. The Google / Microsoft account is bound to the user by its
subject id, so a later change of e-mail at the provider changes nothing here.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from urllib.parse import urlencode

import httpx

import ax_load

ch = ax_load.ch
INVITE_DAYS = 7
RESET_MINUTES = 60
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def app_db() -> str:
    return os.environ.get("APP_DB", "puc_app")


def q(sql: str) -> list[dict]:
    return json.loads(ch(sql + " FORMAT JSON"))["data"]


def esc(s) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def valid_email(s: str) -> bool:
    return len(s) <= 254 and bool(EMAIL_RE.match(s))


def base_url() -> str:
    return os.environ.get("APP_BASE_URL", "").strip().rstrip("/")


# =============================================================================
# Store
# =============================================================================

def init_store() -> None:
    db = app_db()
    for col in ("email String", "google_sub String", "microsoft_sub String", "created_by String"):
        ch(f"ALTER TABLE {db}.users ADD COLUMN IF NOT EXISTS {col}")
    # one row per invitation or reset link; `id` stays the same when an invitation is re-sent
    ch(f"""CREATE TABLE IF NOT EXISTS {db}.tokens (
            id String, kind LowCardinality(String), token_hash String, email String, full_name String,
            role LowCardinality(String), region_code UInt8, username String, created_by String,
            created_at DateTime, expires_at DateTime, status LowCardinality(String), used_by String,
            sent UInt8, updated_at DateTime64(3) DEFAULT now64(3))
          ENGINE = ReplacingMergeTree(updated_at) ORDER BY id""")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def put_token(row: dict) -> None:
    cols = ["id", "kind", "token_hash", "email", "full_name", "role", "region_code", "username", "created_by",
            "created_at", "expires_at", "status", "used_by", "sent"]
    vals = []
    for c in cols:
        v = row.get(c, 0 if c in ("region_code", "sent") else "")
        vals.append(str(int(v)) if c in ("region_code", "sent") else
                    f"toDateTime({int(v)})" if c in ("created_at", "expires_at") else esc(v))
    ch(f"INSERT INTO {app_db()}.tokens ({', '.join(cols)}) VALUES ({', '.join(vals)})")


def _token_rows(where: str) -> list[dict]:
    rows = q(f"SELECT *, toUnixTimestamp(created_at) AS created_ts, toUnixTimestamp(expires_at) AS expires_ts "
             f"FROM {app_db()}.tokens FINAL WHERE {where} ORDER BY created_at DESC")
    for r in rows:
        r["created_at"], r["expires_at"] = int(r.pop("created_ts")), int(r.pop("expires_ts"))
        r["region_code"], r["sent"] = int(r["region_code"]), int(r["sent"])
        r.pop("updated_at", None)
        if r["status"] == "pending" and r["expires_at"] < time.time():
            r["status"] = "expired"
    return rows


def new_token(kind: str, *, email="", full_name="", role="", region=0, username="", by="", minutes: int,
              id_: str | None = None, created_at: int | None = None) -> tuple[str, dict]:
    """A fresh link; returns (token, row). The token itself is never stored."""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    row = {"id": id_ or secrets.token_hex(8), "kind": kind, "token_hash": token_hash(token), "email": email,
           "full_name": full_name, "role": role, "region_code": region, "username": username, "created_by": by,
           "created_at": created_at or now, "expires_at": now + minutes * 60, "status": "pending"}
    put_token(row)
    return token, row


def find_token(kind: str, token: str) -> dict | None:
    """The pending, unexpired link for this token, or None."""
    if not token or len(token) > 100:
        return None
    rows = _token_rows(f"kind = {esc(kind)} AND token_hash = {esc(token_hash(token))}")
    return rows[0] if rows and rows[0]["status"] == "pending" else None


def close_token(row: dict, status: str, used_by: str = "") -> None:
    put_token({**row, "status": status, "used_by": used_by})


def invites() -> list[dict]:
    return [{k: r[k] for k in ("id", "email", "full_name", "role", "region_code", "created_by", "created_at",
                               "expires_at", "status", "used_by", "sent")}
            for r in _token_rows("kind = 'invite'")]


def invite_by_id(id_: str) -> dict | None:
    rows = _token_rows(f"kind = 'invite' AND id = {esc(id_)}")
    return rows[0] if rows else None


# one accept at a time, so one link cannot make two accounts
ACCEPT_LOCK = threading.Lock()


# =============================================================================
# Wrong-password lockout (in memory: a restart forgets it, which is fine)
# =============================================================================

MAX_FAILS = 5
LOCK_SECONDS = 15 * 60
_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()


def locked_for(key: str) -> int:
    """Seconds left on this account's lock, 0 when it may try."""
    with _fails_lock:
        now = time.time()
        recent = [t for t in _fails.get(key, []) if now - t < LOCK_SECONDS]
        _fails[key] = recent
        return int(LOCK_SECONDS - (now - recent[-MAX_FAILS])) + 1 if len(recent) >= MAX_FAILS else 0


def failed(key: str) -> None:
    with _fails_lock:
        _fails.setdefault(key, []).append(time.time())


def clear_fails(key: str) -> None:
    with _fails_lock:
        _fails.pop(key, None)


# password-reset requests per address, so the form cannot be used to flood someone's inbox
_resets: dict[str, list[float]] = {}


def reset_allowed(key: str) -> bool:
    with _fails_lock:
        now = time.time()
        recent = [t for t in _resets.get(key, []) if now - t < 3600]
        if len(recent) >= 3:
            _resets[key] = recent
            return False
        _resets[key] = recent + [now]
        return True


# =============================================================================
# E-mail
# =============================================================================

def mail_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST"))


def send_mail(to: str, subject: str, text: str, html: str) -> None:
    """Raises on failure, with a message fit to show an administrator."""
    if not mail_configured():
        raise RuntimeError("E-mail is not set up (SMTP_HOST in .env)")
    if not valid_email(to):
        raise ValueError("Not an e-mail address")
    host = os.environ["SMTP_HOST"]
    mode = os.environ.get("SMTP_TLS", "starttls").lower()
    port = int(os.environ.get("SMTP_PORT") or (465 if mode == "ssl" else 587 if mode == "starttls" else 25))
    user, password = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM") or user
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("PUC Analytics", sender)) if "<" not in sender else sender
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    try:
        if mode == "ssl":
            s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=20)
        else:
            s = smtplib.SMTP(host, port, timeout=20)
            if mode == "starttls":
                s.starttls(context=ctx)
        with s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError("The mail server refused the SMTP user or password "
                           "(Gmail and Microsoft 365 need an app password)")
    except (OSError, smtplib.SMTPException) as e:
        raise RuntimeError(f"Could not send the e-mail: {e}")


def _mail_html(title: str, lines: list[str], button: str, link: str, foot: str) -> str:
    body = "".join(f'<p style="margin:0 0 14px;color:#334155;line-height:1.55">{line}</p>' for line in lines)
    return f"""<!doctype html><html><body style="margin:0;background:#f1f5f9;font-family:Segoe UI,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:32px 12px"><tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:12px;overflow:hidden">
<tr><td style="background:#123a6b;padding:20px 28px;color:#ffffff;font-size:17px;font-weight:600">PUC Analytics
<div style="font-size:12px;font-weight:400;opacity:.75">Public Utilities Corporation</div></td></tr>
<tr><td style="padding:28px">
<h1 style="margin:0 0 16px;font-size:20px;color:#0f172a">{title}</h1>{body}
<p style="margin:22px 0"><a href="{escape(link)}" style="background:#1f63bd;color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:600;display:inline-block">{button}</a></p>
<p style="margin:0 0 6px;color:#64748b;font-size:12px">Or paste this link into your browser:</p>
<p style="margin:0 0 18px;font-size:12px;word-break:break-all"><a href="{escape(link)}" style="color:#1f63bd">{escape(link)}</a></p>
<p style="margin:0;color:#64748b;font-size:12px">{foot}</p></td></tr></table></td></tr></table></body></html>"""


def invite_mail(to: str, name: str, role_title: str, island: str, by: str, link: str) -> tuple[str, str, str]:
    subject = "You are invited to PUC Analytics"
    hello = f"Hello {name}," if name else "Hello,"
    where = f"{role_title}" + (f", {island}" if island and island != "All islands" else "")
    text = (f"{hello}\n\n{by} invited you to PUC Analytics as {where}.\n\n"
            f"Open this link to create your account (valid {INVITE_DAYS} days, once):\n{link}\n\n"
            "You can choose a username and password, or continue with your Google or Microsoft account "
            f"if it uses this address ({to}).\n\nIf you did not expect this, ignore this e-mail.\n")
    html = _mail_html("You are invited", [escape(hello), f"<b>{escape(by)}</b> invited you to PUC Analytics as "
                      f"<b>{escape(where)}</b>.",
                      "Create your account with a username and password, or continue with your Google or "
                      f"Microsoft account if it uses <b>{escape(to)}</b>."],
                      "Accept the invitation", link,
                      f"The link works once and expires in {INVITE_DAYS} days. If you did not expect this, ignore this e-mail.")
    return subject, text, html


def reset_mail(name: str, username: str, link: str) -> tuple[str, str, str]:
    subject = "Reset your PUC Analytics password"
    text = (f"Hello {name},\n\nSomeone (hopefully you) asked to reset the password of the PUC Analytics account "
            f"'{username}'.\n\nChoose a new password here (valid {RESET_MINUTES} minutes, once):\n{link}\n\n"
            "If you did not ask for this, ignore this e-mail: your password stays as it is.\n")
    html = _mail_html("Reset your password", [f"Hello {escape(name)},",
                      f"Someone (hopefully you) asked to reset the password of the account <b>{escape(username)}</b>."],
                      "Choose a new password", link,
                      f"The link works once and expires in {RESET_MINUTES} minutes. If you did not ask for this, "
                      "ignore this e-mail: your password stays as it is.")
    return subject, text, html


# =============================================================================
# Google / Microsoft sign-in (OpenID Connect, authorization code + PKCE)
# =============================================================================

def providers() -> dict[str, dict]:
    """The providers that have keys in .env. Endpoint overrides (OIDC_<P>_AUTH_URL,
    _TOKEN_URL, _ISSUER) exist for tests and private clouds."""
    out = {}
    if os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
        out["google"] = {
            "title": "Google", "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "auth": os.environ.get("OIDC_GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth"),
            "token": os.environ.get("OIDC_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token"),
            "issuers": [os.environ["OIDC_GOOGLE_ISSUER"]] if os.environ.get("OIDC_GOOGLE_ISSUER")
            else ["https://accounts.google.com", "accounts.google.com"],
        }
    if os.environ.get("MS_CLIENT_ID") and os.environ.get("MS_CLIENT_SECRET"):
        tenant = os.environ.get("MS_TENANT", "organizations")
        root = os.environ.get("OIDC_MICROSOFT_ROOT", "https://login.microsoftonline.com")
        out["microsoft"] = {
            "title": "Microsoft", "client_id": os.environ["MS_CLIENT_ID"], "secret": os.environ["MS_CLIENT_SECRET"],
            "auth": f"{root}/{tenant}/oauth2/v2.0/authorize", "token": f"{root}/{tenant}/oauth2/v2.0/token",
            "tenant": tenant, "root": root,
        }
    return out


def sso_address_ok() -> bool:
    """Google and Microsoft only send people back to an https:// address (or http://localhost)."""
    from urllib.parse import urlparse
    u = urlparse(base_url())
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1"))


def sso_ready() -> dict[str, dict]:
    """Providers that can be used: they need an https:// APP_BASE_URL for the redirect address."""
    return providers() if sso_address_ok() else {}


def redirect_uri(provider: str) -> str:
    return f"{base_url()}/api/auth/oidc/{provider}/callback"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sign(secret: bytes, data: dict) -> str:
    body = _b64(json.dumps(data, separators=(",", ":")).encode())
    return body + "." + _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())


def unsign(secret: bytes, value: str | None) -> dict | None:
    try:
        body, sig = value.split(".")
        if not hmac.compare_digest(sig, _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return data if data.get("exp", 0) > time.time() else None
    except Exception:
        return None


def start(provider: str, secret: bytes, invite: str = "", hint: str = "") -> tuple[str, str]:
    """(URL to send the browser to, signed value for the short-lived flow cookie)."""
    p = sso_ready()[provider]
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    params = {"client_id": p["client_id"], "response_type": "code", "redirect_uri": redirect_uri(provider),
              "scope": "openid email profile", "state": state, "nonce": nonce, "code_challenge": challenge,
              "code_challenge_method": "S256", "prompt": "select_account"}
    if hint:
        params["login_hint"] = hint
    flow = sign(secret, {"p": provider, "s": state, "n": nonce, "v": verifier, "i": invite,
                         "exp": int(time.time()) + 600})
    return p["auth"] + "?" + urlencode(params), flow


class SSOError(Exception):
    pass


def finish(provider: str, flow: dict, code: str, state: str) -> dict:
    """Checks the answer from the provider; returns {sub, email, email_trusted, name}."""
    p = sso_ready().get(provider)
    if not p or not flow or flow.get("p") != provider or not hmac.compare_digest(str(flow.get("s")), state or ""):
        raise SSOError("The sign-in took too long or was started elsewhere. Please try again.")
    try:
        r = httpx.post(p["token"], data={"grant_type": "authorization_code", "code": code,
                                         "redirect_uri": redirect_uri(provider), "client_id": p["client_id"],
                                         "client_secret": p["secret"], "code_verifier": flow["v"]}, timeout=20)
    except httpx.HTTPError:
        raise SSOError(f"Could not reach {p['title']}. Please try again.")
    if r.status_code != 200 or "id_token" not in r.json():
        raise SSOError(f"{p['title']} did not accept the sign-in.")
    # The ID token comes straight from the provider's token endpoint over TLS, in answer to
    # our own client secret, so its signature need not be checked (OpenID Connect Core 3.1.3.7);
    # issuer, audience, expiry and nonce still are.
    try:
        part = r.json()["id_token"].split(".")[1]
        c = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        raise SSOError(f"{p['title']} sent an unreadable answer.")
    aud = c.get("aud")
    if (aud if isinstance(aud, list) else [aud]).count(p["client_id"]) != 1 or c.get("exp", 0) < time.time() - 60 \
            or not hmac.compare_digest(str(c.get("nonce", "")), flow["n"]):
        raise SSOError(f"{p['title']} sent an answer that is not for this app.")
    if provider == "google":
        if c.get("iss") not in p["issuers"]:
            raise SSOError("The answer did not come from Google.")
        email, trusted = str(c.get("email", "")), c.get("email_verified") in (True, "true")
    else:
        tid = str(c.get("tid", ""))
        if not re.fullmatch(r"[0-9a-f-]{36}", tid) or c.get("iss") != f"{p['root']}/{tid}/v2.0" \
                or (p["tenant"] not in ("common", "organizations", "consumers") and p["tenant"] != tid):
            raise SSOError("The answer did not come from Microsoft.")
        email = str(c.get("email") or c.get("preferred_username") or "")
        # a Microsoft address is only proof of ownership within one organisation's own directory
        trusted = p["tenant"] not in ("common", "organizations", "consumers")
    if not c.get("sub"):
        raise SSOError(f"{p['title']} did not say who you are.")
    return {"sub": f"{c.get('tid', '')}:{c['sub']}" if provider == "microsoft" else str(c["sub"]),
            "email": email.strip().lower(), "email_trusted": trusted, "name": str(c.get("name", ""))[:100]}
