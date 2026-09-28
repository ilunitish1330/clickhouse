#!/usr/bin/env python3
"""Sign-in checks: username or e-mail, lockout, the admin "Add user" form,
invitations by e-mail, password reset, and Google / Microsoft sign-in.

    python3 webapp/test_auth.py      # needs ClickHouse; uses its own users database (puc_app_atest)

E-mail goes to a small SMTP server this script runs on 127.0.0.1, and Google /
Microsoft are stood in for by answering the token request here, so nothing
leaves the machine.
"""
import base64
import json
import os
import re
import socketserver
import sys
import threading
import time
from email import message_from_bytes, policy
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]
PW = "Test-pass-1"
os.environ.update({
    "APP_DEFAULT_PASSWORD": PW, "APP_DB": "puc_app_atest", "APP_BASE_URL": "https://puc.example.test",
    "SMTP_HOST": "127.0.0.1", "SMTP_PORT": "2525", "SMTP_TLS": "none", "SMTP_FROM": "analytics@puc.example.test",
    "GOOGLE_CLIENT_ID": "g-client", "GOOGLE_CLIENT_SECRET": "g-secret",
    "MS_CLIENT_ID": "m-client", "MS_CLIENT_SECRET": "m-secret", "MS_TENANT": "organizations",
})
for k in ("SMTP_USER", "SMTP_PASSWORD"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import server  # noqa: E402

server.ch("DROP DATABASE IF EXISTS puc_app_atest")
OUTBOX: list = []
CHECKS = [0]


def ok(cond, what):
    CHECKS[0] += 1
    assert cond, what


# --- a tiny SMTP server ------------------------------------------------------------

class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        say = lambda s: self.wfile.write((s + "\r\n").encode())  # noqa: E731
        say("220 test")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode().strip().upper()
            if cmd.startswith(("EHLO", "HELO")):
                say("250 test")
            elif cmd.startswith("DATA"):
                say("354 go")
                data = b""
                while (chunk := self.rfile.readline()) != b".\r\n":
                    data += chunk[1:] if chunk.startswith(b"..") else chunk
                OUTBOX.append(message_from_bytes(data, policy=policy.default))
                say("250 queued")
            elif cmd.startswith("QUIT"):
                say("221 bye")
                return
            else:
                say("250 ok")


socketserver.ThreadingTCPServer.allow_reuse_address = True
smtp = socketserver.ThreadingTCPServer(("127.0.0.1", 2525), SMTPHandler)
threading.Thread(target=smtp.serve_forever, daemon=True).start()


def last_code(to: str) -> str:
    msg = [m for m in OUTBOX if m["To"] == to and m["Subject"].endswith("is your PUC Analytics code")][-1]
    return msg["Subject"].split()[0]


def send_code(c, token: str, to: str) -> str:
    auth._code_sends.clear()  # the 30-second gap between codes is checked on its own below
    r = c.post(f"/api/invite/{token}/code")
    ok(r.status_code == 200 and r.json()["email"] == to, r.text)
    return last_code(to)


def last_link(to: str) -> str:
    msg = [m for m in OUTBOX if m["To"] == to][-1]
    text = msg.get_body(("plain",)).get_content()
    return re.search(r"https://puc\.example\.test/#/\S+", text).group(0)


# --- stand-in Google / Microsoft ---------------------------------------------------

ID_CLAIMS: dict = {}
TOKEN_CALLS: list = []


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def fake_post(url, data=None, timeout=None):
    TOKEN_CALLS.append((url, data))
    claims = ID_CLAIMS.get(data["code"])
    if not claims:
        return FakeResp(400, {"error": "invalid_grant"})
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return FakeResp(200, {"id_token": f"{part({'alg': 'RS256'})}.{part(claims)}.sig", "access_token": "x"})


auth.httpx.post = fake_post
TENANT = "11111111-2222-3333-4444-555555555555"


def sso(c: TestClient, provider: str, claims: dict, invite: str = "", tamper=None):
    """Runs the redirect dance; returns (final location, the params sent to the provider)."""
    r = c.get(f"/api/auth/oidc/{provider}/start", params={"invite": invite} if invite else {}, follow_redirects=False)
    ok(r.status_code == 303, r.text)
    sent = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    if "state" not in sent:
        return r.headers["location"], sent
    code = "code-" + str(len(ID_CLAIMS))
    base = {"aud": sent["client_id"], "exp": time.time() + 300, "nonce": sent["nonce"]}
    if provider == "google":
        base.update(iss="https://accounts.google.com", email_verified=True)
    else:
        base.update(iss=f"https://login.microsoftonline.com/{TENANT}/v2.0", tid=TENANT)
    ID_CLAIMS[code] = {**base, **claims}
    state = sent["state"]
    if tamper == "state":
        state = "wrong"
    if tamper == "cookie":
        c.cookies.delete("puc_sso", path="/api/auth/oidc")
    r = c.get(f"/api/auth/oidc/{provider}/callback", params={"code": code, "state": state}, follow_redirects=False)
    ok(r.status_code == 303, r.text)
    return r.headers["location"], sent


def client() -> TestClient:
    c = TestClient(server.app, base_url="https://puc.example.test")
    c.__enter__()
    return c


def signed_in(c) -> str | None:
    r = c.get("/api/me")
    return r.json()["username"] if r.status_code == 200 else None


def main() -> None:
    admin = client()
    ok(admin.post("/api/login", json={"username": "admin", "password": PW}).status_code == 200, "admin signs in")
    ok(not admin.get("/api/me").json()["restart_needed"], "no restart warning when the server runs the code on disk")
    saved_version, server.VERSION = server.VERSION, "0000000 2000-01-01"
    ok(admin.get("/api/me").json()["restart_needed"] or not saved_version, "warning when the code on disk is newer")
    server.VERSION = saved_version
    cfg = admin.get("/api/auth/config").json()
    ok([p["id"] for p in cfg["sso"]] == ["google", "microsoft"] and cfg["forgot"], cfg)

    # ---- the "Add user" form ----------------------------------------------------------------
    r = admin.post("/api/users", json={"new": True, "username": "jane.doe", "full_name": "Jane Doe",
                                        "email": "Jane.Doe@Gmail.com", "role": "finance", "region_code": 0,
                                        "password": "Start-pass-1", "active": True})
    ok(r.status_code == 200, r.text)
    u = next(u for u in admin.get("/api/users").json()["users"] if u["username"] == "jane.doe")
    ok(u["email"] == "jane.doe@gmail.com" and u["created_by"] == "admin" and +u["must_change"] == 1, u)
    bad = [({"username": "jane.doe"}, "taken"), ({"username": "x y"}, "Username"), ({"email": "not-mail"}, "valid e-mail"),
           ({"email": "jane.doe@gmail.com", "username": "other"}, "belongs to"), ({"password": "short"}, "8 characters"),
           ({"role": "king"}, "Unknown role"), ({"role": "regional_manager"}, "island")]
    for change, msg in bad:
        body = {"new": True, "username": "someone", "full_name": "S", "email": "", "role": "finance", "region_code": 0,
                "password": "Start-pass-1", "active": True, **change}
        r = admin.post("/api/users", json=body)
        ok(r.status_code == 400 and msg.lower() in r.text.lower(), (change, r.text))

    # sign in by username or by e-mail (any case)
    jane = client()
    ok(jane.post("/api/login", json={"username": "JANE.DOE@gmail.com", "password": "Start-pass-1"}).status_code == 200,
       "e-mail sign-in")
    ok(jane.get("/api/me").json()["must_change"], "first password must be changed")

    # a password change ends other sessions but keeps this one
    other = client()
    ok(other.post("/api/login", json={"username": "jane.doe", "password": "Start-pass-1"}).status_code == 200, "2nd")
    ok(jane.post("/api/me/password", json={"current": "Start-pass-1", "new": "Jane-pass-22"}).status_code == 200, "pw")
    ok(signed_in(jane) == "jane.doe", "this session goes on")
    ok(signed_in(other) is None, "the other session ended")

    # ---- several roles per user ------------------------------------------------------------------
    def make(username, roles, region=0):
        r = admin.post("/api/users", json={"new": True, "username": username, "full_name": username.title(),
                                            "email": "", "roles": roles, "region_code": region,
                                            "password": "Multi-pass-1", "active": True})
        ok(r.status_code == 200, r.text)
        c = client()
        ok(c.post("/api/login", json={"username": username, "password": "Multi-pass-1"}).status_code == 200, username)
        return c, c.get("/api/me").json()
    c, me = make("elec.reviewer", ["electricity_manager", "reviewer"])
    ok(me["roles"] == ["electricity_manager", "reviewer"], me["roles"])
    ok(me["role_title"] == "Electricity Division Manager + Data Reviewer", me["role_title"])
    pages = [p["id"] for p in me["pages"]]
    ok("solar" in pages and "water" in pages and me["can_review"] and not me["can_admin"], pages)
    ok(c.get("/api/review/batches").status_code == 200, "the reviewer part works")
    ok(c.get("/api/users").status_code == 403, "neither role is an admin")
    c, me = make("utility.pair", ["electricity_manager", "water_manager"])
    ok([u["code"] for u in me["filters"]["utilities"]] == [1, 2, 3], me["filters"]["utilities"])
    ok(not me["can_review"] and "review" not in [p["id"] for p in me["pages"]], "no review from either role")
    c, me = make("ops.billing", ["data_operator", "billing_officer"], region=3)
    ok(me["can_run"] and me["island"] == "La Digue" and [p["id"] for p in me["pages"]] == ["customers", "controls", "consumption"], me)
    listed = next(u for u in admin.get("/api/users").json()["users"] if u["username"] == "utility.pair")
    ok(listed["roles"] == ["electricity_manager", "water_manager"], listed)
    for roles, msg in [([], "at least one role"), (["finance", "king"], "Unknown role"),
                       (["finance", "regional_manager"], "island")]:
        r = admin.post("/api/users", json={"new": True, "username": "bad.roles", "full_name": "B", "roles": roles,
                                            "region_code": 0, "password": "Multi-pass-1", "active": True})
        ok(r.status_code == 400 and msg.lower() in r.text.lower(), (roles, r.text))
    # an administrator keeps the admin role on their own account, with or without others
    me_row = next(u for u in admin.get("/api/users").json()["users"] if u["username"] == "admin")
    r = admin.post("/api/users", json={**me_row, "password": None, "active": True, "roles": ["finance"]})
    ok(r.status_code == 400 and "demote" in r.text, r.text)
    r = admin.post("/api/users", json={**me_row, "password": None, "active": True, "roles": ["admin", "reviewer"]})
    ok(r.status_code == 200 and admin.get("/api/me").json()["can_admin"], r.text)
    # an invitation with two roles
    inv = admin.post("/api/invites", json={"email": "two.roles@x.test", "roles": ["auditor", "reviewer"],
                                            "region_code": 0}).json()
    tok = inv["link"].rsplit("/", 1)[1]
    d = client().get(f"/api/invite/{tok}").json()
    ok([r["id"] for r in d["roles"]] == ["auditor", "reviewer"], d)
    ok("Internal Auditor + Data Reviewer" in [m for m in OUTBOX if m["To"] == "two.roles@x.test"][-1]
       .get_body(("plain",)).get_content(), "both roles in the e-mail")
    c = client()
    code = send_code(c, tok, "two.roles@x.test")
    ok(c.post(f"/api/invite/{tok}/accept", json={"username": "two.roles", "full_name": "Two Roles",
                                                 "password": "Two-pass-11", "code": code}).status_code == 200, "accept")
    me = c.get("/api/me").json()
    ok(me["roles"] == ["auditor", "reviewer"] and me["can_review"] and me["can_load"], me)

    # ---- lockout ------------------------------------------------------------------------------
    anon = client()
    for i in range(5):
        ok(anon.post("/api/login", json={"username": "jane.doe", "password": "nope"}).status_code == 401, i)
    r = anon.post("/api/login", json={"username": "jane.doe", "password": "Jane-pass-22"})
    ok(r.status_code == 429 and "Too many" in r.text, r.text)
    r = anon.post("/api/login", json={"username": "jane.doe@gmail.com", "password": "Jane-pass-22"})
    ok(r.status_code == 429, "the lock follows the account, not what was typed")
    ok(next(u for u in admin.get("/api/users").json()["users"] if u["username"] == "jane.doe")["locked"], "shown")
    u = next(u for u in admin.get("/api/users").json()["users"] if u["username"] == "jane.doe")
    admin.post("/api/users", json={**u, "password": None, "active": True})  # the admin saving lifts it
    ok(anon.post("/api/login", json={"username": "jane.doe", "password": "Jane-pass-22"}).status_code == 200, "unlock")

    # ---- forgot password --------------------------------------------------------------------------
    n = len(OUTBOX)
    anon = client()
    r1 = anon.post("/api/auth/forgot", json={"username": "jane.doe@gmail.com"})
    r2 = anon.post("/api/auth/forgot", json={"username": "nobody@nowhere.test"})
    ok(r1.status_code == r2.status_code == 200 and r1.json() == r2.json(), "same answer either way")
    ok(len(OUTBOX) == n + 1 and OUTBOX[-1]["To"] == "jane.doe@gmail.com", "one mail, to Jane")
    link = last_link("jane.doe@gmail.com")
    ok(link.startswith("https://puc.example.test/#/reset/"), link)
    token = link.rsplit("/", 1)[1]
    ok(anon.get(f"/api/auth/reset/{token}").json()["username"] == "jane.doe", "reset link")
    ok(anon.post(f"/api/auth/reset/{token}", json={"password": "short"}).status_code == 400, "short")
    ok(anon.post(f"/api/auth/reset/{token}", json={"password": "Jane-reset-3"}).status_code == 200, "reset")
    ok(signed_in(anon) == "jane.doe", "signed in after reset")
    ok(signed_in(jane) is None, "the old session ended")
    ok(anon.post(f"/api/auth/reset/{token}", json={"password": "Jane-reset-4"}).status_code == 404, "single use")
    ok(anon.get("/api/auth/reset/garbage").status_code == 404, "bad token")
    for _ in range(3):
        anon.post("/api/auth/forgot", json={"username": "jane.doe"})
    ok(len([m for m in OUTBOX if m["To"] == "jane.doe@gmail.com"]) == 3, "at most 3 reset mails an hour")
    # the link comes from APP_BASE_URL, whatever Host the request says
    evil = TestClient(server.app, base_url="https://evil.example")
    evil.__enter__()
    auth._resets.clear()
    evil.post("/api/auth/forgot", json={"username": "jane.doe"}, headers={"Host": "evil.example"})
    ok(last_link("jane.doe@gmail.com").startswith("https://puc.example.test/"), "no host poisoning")

    # ---- invitations ------------------------------------------------------------------------------------
    anon = client()
    ok(anon.post("/api/invites", json={"email": "a@b.test", "role": "finance"}).status_code == 401, "admins only")
    ok(jane.get("/api/invites").status_code == 401 and anon.get("/api/invites").status_code == 401, "admins only")
    fin = client()
    fin.post("/api/login", json={"username": "finance", "password": PW})
    ok(fin.post("/api/invites", json={"email": "a@b.test", "role": "finance"}).status_code == 403, "not finance")
    for body, msg in [({"email": "bad"}, "valid e-mail"), ({"email": "jane.doe@gmail.com"}, "already exists"),
                      ({"role": "nope"}, "Unknown role"), ({"role": "regional_manager"}, "island")]:
        r = admin.post("/api/invites", json={"email": "new@x.test", "role": "finance", "region_code": 0, **body})
        ok(r.status_code == 400 and msg.lower() in r.text.lower(), (body, r.text))

    r = admin.post("/api/invites", json={"email": "Sam.Lee@Outlook.com", "full_name": "Sam Lee",
                                          "role": "regional_manager", "region_code": 2})
    inv = r.json()
    ok(r.status_code == 200 and inv["sent"] and inv["link"].startswith("https://puc.example.test/#/invite/"), inv)
    mail = OUTBOX[-1]
    ok(mail["To"] == "sam.lee@outlook.com" and "invited" in mail["Subject"].lower(), mail["Subject"])
    html = mail.get_body(("html",)).get_content()
    ok("Regional manager" in html or "regional" in html.lower(), "the role is in the mail")
    ok(last_link("sam.lee@outlook.com") == inv["link"], "the admin sees the same link")
    token = inv["link"].rsplit("/", 1)[1]
    listed = admin.get("/api/invites").json()["invites"]
    ok(listed[0]["email"] == "sam.lee@outlook.com" and listed[0]["status"] == "pending" and listed[0]["sent"], listed[0])
    ok(all("token" not in k for k in listed[0]), "no token or hash in the list")

    anon = client()
    d = anon.get(f"/api/invite/{token}").json()
    ok(d["email"] == "sam.lee@outlook.com" and d["island"] == "Praslin" and len(d["sso"]) == 2 and d["needs_code"], d)
    ok("works only for sam.lee@outlook.com" in mail.get_body(("plain",)).get_content(), "the e-mail says so")
    # a forwarded link alone is not enough: the code goes to the invited address only
    body = {"username": "sam", "full_name": "Sam Lee", "password": "Sam-pass-11"}
    r = anon.post(f"/api/invite/{token}/accept", json=body)
    ok(r.status_code == 400 and "send the code" in r.text, r.text)
    r = anon.post(f"/api/invite/{token}/accept", json={**body, "code": "123456"})
    ok(r.status_code == 400 and "send the code" in r.text, "a guess before any code was sent")
    n = len(OUTBOX)
    code = send_code(anon, token, "sam.lee@outlook.com")
    ok(len(OUTBOX) == n + 1 and OUTBOX[-1]["To"] == "sam.lee@outlook.com" and re.fullmatch(r"\d{6}", code), "code mailed")
    r = anon.post(f"/api/invite/{token}/code")
    ok(r.status_code == 429 and "seconds" in r.text, "not twice in 30 seconds")
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    r = anon.post(f"/api/invite/{token}/accept", json={**body, "code": wrong})
    ok(r.status_code == 400 and "4 tries left" in r.text, r.text)
    for extra, msg in [({"username": "bad name"}, "Username"), ({"password": "short"}, "8 characters"),
                       ({"username": "jane.doe"}, "taken"), ({"full_name": ""}, "full name")]:
        r = anon.post(f"/api/invite/{token}/accept", json={**body, "code": code, **extra})
        ok(r.status_code == 400 and msg.lower() in r.text.lower(), (extra, r.text))
    r = anon.post(f"/api/invite/{token}/accept", json={**body, "username": "Sam", "code": f" {code[:3]} {code[3:]} "})
    ok(r.status_code == 200, r.text)
    me = anon.get("/api/me").json()
    ok(me["username"] == "sam" and me["role"] == "regional_manager" and me["island"] == "Praslin"
       and not me["must_change"] and me["email"] == "sam.lee@outlook.com", me)
    ok(client().post(f"/api/invite/{token}/accept", json={"username": "sam2", "full_name": "S",
                                                         "password": "Sam-pass-11"}).status_code == 404, "used once")
    ok(client().get(f"/api/invite/{token}").status_code == 404, "used once")
    ok(admin.get("/api/invites").json()["invites"][0]["status"] == "accepted", "accepted")
    ok(admin.post("/api/invites", json={"email": "sam.lee@outlook.com", "role": "finance"}).status_code == 400, "exists")

    # wrong codes: five and the code is dead; a new code works; another invitation's code never does
    ia = admin.post("/api/invites", json={"email": "ana@x.test", "role": "auditor"}).json()["link"].rsplit("/", 1)[1]
    ib = admin.post("/api/invites", json={"email": "ben@x.test", "role": "auditor"}).json()["link"].rsplit("/", 1)[1]
    c = client()
    code_a, code_b = send_code(c, ia, "ana@x.test"), send_code(c, ib, "ben@x.test")
    body = {"username": "ana", "full_name": "Ana", "password": "Ana-pass-11"}
    if code_b != code_a:
        r = c.post(f"/api/invite/{ia}/accept", json={**body, "code": code_b})
        ok(r.status_code == 400 and "not right" in r.text, "a code from another invitation")
    for _ in range(5):
        r = c.post(f"/api/invite/{ia}/accept", json={**body, "code": "000000" if code_a != "000000" else "111111"})
    ok(r.status_code == 400 and "Too many wrong codes" in r.text, r.text)
    r = c.post(f"/api/invite/{ia}/accept", json={**body, "code": code_a})
    ok(r.status_code == 400 and "Too many wrong codes" in r.text, "the right code is dead after five wrong ones")
    code_a = send_code(c, ia, "ana@x.test")
    row = auth.find_token("invite", ia)
    auth.put_token({**row, "code_expires": int(time.time()) - 1})
    r = c.post(f"/api/invite/{ia}/accept", json={**body, "code": code_a})
    ok(r.status_code == 400 and "expired" in r.text, "an old code")
    code_a = send_code(c, ia, "ana@x.test")
    ok(c.post(f"/api/invite/{ia}/accept", json={**body, "code": code_a}).status_code == 200, "a fresh code works")
    ok(client().post(f"/api/invite/{ia}/code").status_code == 404, "no codes for a used invitation")
    auth._code_sends.clear()
    for _ in range(5):
        auth._code_sends.setdefault(auth.find_token("invite", ib)["id"], []).append(time.time() - 60)
    r = client().post(f"/api/invite/{ib}/code")
    ok(r.status_code == 429, "at most 5 codes an hour")
    auth._code_sends.clear()

    # resend replaces the link; revoke closes it; a new invitation to the same address replaces the old
    inv = admin.post("/api/invites", json={"email": "kim@x.test", "role": "auditor"}).json()
    old = inv["link"].rsplit("/", 1)[1]
    iid = next(i["id"] for i in admin.get("/api/invites").json()["invites"] if i["email"] == "kim@x.test")
    again = admin.post(f"/api/invites/{iid}/resend").json()
    new = again["link"].rsplit("/", 1)[1]
    ok(new != old and client().get(f"/api/invite/{old}").status_code == 404 and
       client().get(f"/api/invite/{new}").status_code == 200, "resend: old link dead, new link works")
    ok(len([i for i in admin.get("/api/invites").json()["invites"] if i["email"] == "kim@x.test"]) == 1, "same row")
    ok(admin.post(f"/api/invites/{iid}/revoke").status_code == 200, "revoke")
    ok(client().get(f"/api/invite/{new}").status_code == 404, "revoked link dead")
    ok(admin.post(f"/api/invites/{iid}/revoke").status_code == 400, "revoke twice")
    a = admin.post("/api/invites", json={"email": "lee@x.test", "role": "auditor"}).json()["link"].rsplit("/", 1)[1]
    b = admin.post("/api/invites", json={"email": "lee@x.test", "role": "finance"}).json()["link"].rsplit("/", 1)[1]
    ok(client().get(f"/api/invite/{a}").status_code == 404 and client().get(f"/api/invite/{b}").json()["role"] == "finance",
       "a new invitation replaces the open one")
    # an expired link
    row = auth.find_token("invite", b)
    auth.put_token({**row, "expires_at": int(time.time()) - 5})
    ok(client().get(f"/api/invite/{b}").status_code == 404, "expired")
    ok(next(i for i in admin.get("/api/invites").json()["invites"] if i["id"] == row["id"])["status"] == "expired", "shown expired")

    # without e-mail set up the admin still gets the link
    saved = os.environ.pop("SMTP_HOST")
    inv = admin.post("/api/invites", json={"email": "nomail@x.test", "role": "auditor"}).json()
    ok(not inv["sent"] and "copy the link" in inv["error"] and "/#/invite/" in inv["link"], inv)
    ok(client().post("/api/auth/forgot", json={"username": "jane.doe"}).status_code == 400, "no reset without mail")
    ok(not client().get("/api/auth/config").json()["forgot"], "no Forgot link without mail")
    os.environ["SMTP_HOST"] = saved
    r = admin.post("/api/mail/test", json={"to": "ops@x.test"})
    ok(r.status_code == 200 and OUTBOX[-1]["To"] == "ops@x.test", r.text)
    ok(admin.post("/api/mail/test", json={"to": "bad"}).status_code == 400, "bad test address")

    # ---- Google / Microsoft ----------------------------------------------------------------------------
    inv = admin.post("/api/invites", json={"email": "ravi@gmail.com", "full_name": "Ravi", "role": "auditor"}).json()
    token = inv["link"].rsplit("/", 1)[1]
    c = client()
    loc, sent = sso(c, "google", {"sub": "g-111", "email": "someone.else@gmail.com"}, invite=token)
    ok("signin_error" in loc and "ravi@gmail.com" in loc and signed_in(c) is None, "wrong account refused")
    ok(sent["login_hint"] == "ravi@gmail.com" and sent["code_challenge_method"] == "S256"
       and sent["redirect_uri"] == "https://puc.example.test/api/auth/oidc/google/callback", sent)
    loc, _ = sso(c, "google", {"sub": "g-111", "email": "ravi@gmail.com"}, invite=token)
    ok(loc == "/#/" and signed_in(c) == "ravi", (loc, signed_in(c)))
    ok(TOKEN_CALLS[-1][1]["code_verifier"] and TOKEN_CALLS[-1][1]["client_secret"] == "g-secret", "PKCE + secret")
    u = next(u for u in admin.get("/api/users").json()["users"] if u["username"] == "ravi")
    ok(+u["google"] and not +u["has_password"] and u["role"] == "auditor", u)
    ok(client().post("/api/login", json={"username": "ravi", "password": ""}).status_code == 401, "no empty password")
    # later sign-ins match the Google account, even if its address changed
    c2 = client()
    loc, _ = sso(c2, "google", {"sub": "g-111", "email": "ravi.new@gmail.com"})
    ok(loc == "/#/" and signed_in(c2) == "ravi", loc)
    # an unknown Google account is turned away
    c3 = client()
    loc, _ = sso(c3, "google", {"sub": "g-999", "email": "stranger@gmail.com"})
    ok("No+PUC+Analytics+account" in loc and signed_in(c3) is None, loc)
    # an existing user whose verified Google address matches is linked on first use
    loc, _ = sso(c3, "google", {"sub": "g-222", "email": "jane.doe@gmail.com"})
    ok(loc == "/#/" and signed_in(c3) == "jane.doe", loc)
    # ... but not with an unverified address
    c4 = client()
    loc, _ = sso(c4, "google", {"sub": "g-333", "email": "sam.lee@outlook.com", "email_verified": False})
    ok(signed_in(c4) is None, "unverified address")
    # tampering
    for tamper in ("state", "cookie"):
        c5 = client()
        loc, _ = sso(c5, "google", {"sub": "g-111", "email": "ravi@gmail.com"}, tamper=tamper)
        ok("signin_error" in loc and signed_in(c5) is None, tamper)
    for bad in ({"aud": "someone-else"}, {"iss": "https://evil.example"}, {"exp": time.time() - 3600},
                {"nonce": "replayed"}):
        c5 = client()
        loc, _ = sso(c5, "google", {"sub": "g-111", "email": "ravi@gmail.com", **bad})
        ok("signin_error" in loc and signed_in(c5) is None, bad)
    r = client().get("/api/auth/oidc/github/start", follow_redirects=False)
    ok(r.status_code == 404, "unknown provider")

    # Microsoft: with a multi-tenant app the address is not proven, so it cannot accept an invitation ...
    inv = admin.post("/api/invites", json={"email": "mia@contoso.test", "role": "billing_officer"}).json()
    token = inv["link"].rsplit("/", 1)[1]
    c = client()
    loc, sent = sso(c, "microsoft", {"sub": "m-1", "preferred_username": "Mia@Contoso.test", "name": "Mia Wong"},
                    invite=token)
    ok("did+not+confirm" in loc and signed_in(c) is None, loc)
    # ... with the app limited to one organisation's directory it can
    os.environ["MS_TENANT"] = TENANT
    loc, sent = sso(c, "microsoft", {"sub": "m-1", "preferred_username": "Mia@Contoso.test", "name": "Mia Wong"},
                    invite=token)
    ok(loc == "/#/" and signed_in(c) == "mia", loc)
    os.environ["MS_TENANT"] = "organizations"
    ok(c.get("/api/me").json()["full_name"] == "Mia Wong", "name from Microsoft")
    c6 = client()
    loc, _ = sso(c6, "microsoft", {"sub": "m-2", "email": "jane.doe@gmail.com"})
    ok(signed_in(c6) is None, "multi-tenant Microsoft address does not link an account")
    c7 = client()
    loc, _ = sso(c7, "microsoft", {"sub": "m-1", "iss": "https://login.microsoftonline.com/other/v2.0"})
    ok(signed_in(c7) is None, "wrong Microsoft issuer")
    c8 = client()
    loc, _ = sso(c8, "microsoft", {"sub": "m-1"})
    ok(signed_in(c8) == "mia", "Mia again, by her Microsoft account")

    # a disabled account cannot use Google either
    u = next(u for u in admin.get("/api/users").json()["users"] if u["username"] == "ravi")
    admin.post("/api/users", json={**u, "password": None, "active": False})
    c9 = client()
    loc, _ = sso(c9, "google", {"sub": "g-111", "email": "ravi@gmail.com"})
    ok("disabled" in loc and signed_in(c9) is None and signed_in(c2) is None, loc)

    # without APP_BASE_URL there is no Google / Microsoft (they need the fixed return address)
    saved = os.environ.pop("APP_BASE_URL")
    ok(client().get("/api/auth/config").json()["sso"] == [], "no SSO without APP_BASE_URL")
    # ... nor on a plain http:// address, which they refuse (http://localhost is allowed)
    os.environ["APP_BASE_URL"] = "http://192.168.11.71:8020"
    ok(client().get("/api/auth/config").json()["sso"] == [], "no SSO on http://")
    os.environ["APP_BASE_URL"] = "http://localhost:8020"
    ok(len(client().get("/api/auth/config").json()["sso"]) == 2, "SSO on http://localhost")
    os.environ["APP_BASE_URL"] = saved

    # the audit trail
    acts = {r["action"] for r in server.q("SELECT DISTINCT action FROM puc_app_atest.audit")}
    for a in ("login", "login_failed", "login_locked", "invite_sent", "invite_accepted", "invite_revoked",
              "invite_resent", "password_reset", "sso_linked"):
        ok(a in acts, a)
    print(f"auth ok ({CHECKS[0]} checks, {len(OUTBOX)} e-mails)")


if __name__ == "__main__":
    try:
        main()
    finally:
        smtp.shutdown()
        server.ch("DROP DATABASE IF EXISTS puc_app_atest")
