#!/usr/bin/env python3
"""Sets up e-mail and Google / Microsoft sign-in for the web app: asks for each
setting, writes them into the repository's .env, and checks that each one works.

    python3 webapp/setup_signin.py            # the questions, then the checks
    python3 webapp/setup_signin.py --check    # only the checks, on the current .env

Press Enter to keep what .env already has (shown in brackets); passwords and
secrets are typed hidden and never printed. Restart the web app afterwards.

What only you can do, in your own accounts (the wizard tells you when):
  - Gmail: an app password (https://myaccount.google.com/apppasswords)
  - Microsoft 365: "Authenticated SMTP" allowed for the sending mailbox
  - Google: an OAuth client;  Microsoft: an app registration
"""
import getpass
import os
import re
import secrets
import shutil
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ENV = HERE.parent / ".env"
sys.path[:0] = [str(HERE.parent), str(HERE)]

SECRET_KEYS = {"SMTP_PASSWORD", "GOOGLE_CLIENT_SECRET", "MS_CLIENT_SECRET", "APP_SECRET"}


# --- .env ---------------------------------------------------------------------------

def read_env() -> dict:
    out = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env(values: dict) -> None:
    """Changes the lines for these keys in place and appends the rest; keeps everything else."""
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    if ENV.exists():
        shutil.copy2(ENV, ENV.with_name(".env.bak"))
        ENV.with_name(".env.bak").chmod(0o600)  # it holds the old secrets
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in values:
            lines[i] = f"{m.group(1)}={values[m.group(1)]}"
            done.add(m.group(1))
    rest = [k for k in values if k not in done]
    if rest:
        lines += ["", "# ---- web app sign-in (written by webapp/setup_signin.py)"] + [f"{k}={values[k]}" for k in rest]
    ENV.write_text("\n".join(lines) + "\n")
    ENV.chmod(0o600)


# --- asking -------------------------------------------------------------------------

def ask(prompt: str, current: str = "", *, hidden=False, check=None, allow_empty=True) -> str:
    while True:
        shown = ("(set, Enter keeps it)" if current else "") if hidden else current
        label = f"  {prompt}" + (f" [{shown}]" if shown else "") + ": "
        value = (getpass.getpass(label) if hidden else input(label)).strip()
        value = value or current
        if not value and not allow_empty:
            print("    needed")
            continue
        if value and check:
            problem = check(value)
            if problem:
                print(f"    {problem}")
                continue
        if value and value[-1] in "'\"":
            print("    a value may not end with a quote character (.env would drop it)")
            continue
        return value


def yes(prompt: str, default: bool) -> bool:
    a = input(f"  {prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not a else a.startswith("y")


def choose(prompt: str, options: list[tuple[str, str]], default: str) -> str:
    print(f"  {prompt}")
    for i, (key, text) in enumerate(options, 1):
        print(f"    {i}. {text}{'  (current)' if key == default else ''}")
    while True:
        a = input(f"  Choose 1-{len(options)} [{[k for k, _ in options].index(default) + 1}]: ").strip()
        if not a:
            return default
        if a.isdigit() and 1 <= int(a) <= len(options):
            return options[int(a) - 1][0]


def check_url(v: str) -> str:
    u = urlparse(v)
    if u.scheme not in ("http", "https") or not u.netloc or u.path not in ("", "/") or u.query:
        return "like https://analytics.puc.sc or http://192.168.1.20:8020 (no path)"
    return ""


def check_email(v: str) -> str:
    import auth
    return "" if auth.valid_email(v.lower()) else "not an e-mail address"


def guess_url() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        ip = socket.gethostname()
    return f"http://{ip}:{os.environ.get('APP_PORT', '8020')}"


def wizard() -> dict:
    env = read_env()
    new = {}
    print(f"\nSign-in setup -- writes {ENV}\n")

    print("1. The address people open")
    print("   E-mailed links use it. Google and Microsoft sign-in need it to be https://")
    new["APP_BASE_URL"] = ask("Address", env.get("APP_BASE_URL") or guess_url(), check=check_url).rstrip("/")
    https = new["APP_BASE_URL"].startswith("https://") or urlparse(new["APP_BASE_URL"]).hostname == "localhost"

    print("\n2. E-mail for invitations and password resets")
    cur = env.get("SMTP_HOST", "")
    kind = choose("Send from:", [("gmail", "Gmail (smtp.gmail.com)"), ("m365", "Microsoft 365 / Outlook (smtp.office365.com)"),
                                 ("other", "Another mail server"), ("none", "No e-mail for now (copy invitation links by hand)")],
                  {"smtp.gmail.com": "gmail", "smtp.office365.com": "m365", "": "none"}.get(cur, "other"))
    if kind == "none":
        new.update(SMTP_HOST="")
    else:
        if kind == "gmail":
            print("   Gmail refuses your normal password here. With 2-Step Verification on, make an app password at\n"
                  "   https://myaccount.google.com/apppasswords and paste its 16 letters (spaces are removed).\n"
                  "   If that page says \"The setting you are looking for is not available for your account\":\n"
                  "   turn on 2-Step Verification first (https://myaccount.google.com/signinoptions/twosv), then open\n"
                  "   the page again. A company (Google Workspace) account needs its admin to allow it.")
            new.update(SMTP_HOST="smtp.gmail.com", SMTP_PORT="587", SMTP_TLS="starttls")
        elif kind == "m365":
            print("   Your Microsoft 365 admin must allow \"Authenticated SMTP\" for this mailbox (admin centre > Users >\n"
                  "   the mailbox > Mail > Manage email apps). With MFA on, use an app password.")
            new.update(SMTP_HOST="smtp.office365.com", SMTP_PORT="587", SMTP_TLS="starttls")
        else:
            new["SMTP_HOST"] = ask("Mail server", cur if cur not in ("smtp.gmail.com", "smtp.office365.com") else "", allow_empty=False)
            new["SMTP_TLS"] = choose("Encryption:", [("starttls", "STARTTLS (usually port 587)"), ("ssl", "SSL/TLS (usually port 465)"),
                                                     ("none", "None (only inside a trusted network)")], env.get("SMTP_TLS", "starttls"))
            new["SMTP_PORT"] = ask("Port", env.get("SMTP_PORT") or {"starttls": "587", "ssl": "465", "none": "25"}[new["SMTP_TLS"]],
                                   check=lambda v: "" if v.isdigit() else "a number")
        new["SMTP_USER"] = ask("Sending account (e-mail)", env.get("SMTP_USER", ""), check=check_email,
                               allow_empty=kind == "other")
        pw = ask("Password / app password", env.get("SMTP_PASSWORD", ""), hidden=True, allow_empty=kind == "other")
        new["SMTP_PASSWORD"] = pw.replace(" ", "") if kind == "gmail" else pw
        new["SMTP_FROM"] = ask("From address", env.get("SMTP_FROM") or new["SMTP_USER"], check=check_email)

    base = new["APP_BASE_URL"]
    print("\n3. \"Continue with Google\"")
    if not https:
        print("   Needs an https:// address (Google refuses http except localhost) -- skipped for now.")
    if https and yes("Set up Google sign-in?", bool(env.get("GOOGLE_CLIENT_ID"))):
        print("   In https://console.cloud.google.com : APIs & Services > OAuth consent screen (set it up once), then\n"
              "   Credentials > Create credentials > OAuth client ID > Web application, with this redirect URI:\n"
              f"      {base}/api/auth/oidc/google/callback")
        new["GOOGLE_CLIENT_ID"] = ask("Client ID", env.get("GOOGLE_CLIENT_ID", ""), allow_empty=False,
                                      check=lambda v: "" if v.endswith(".apps.googleusercontent.com") else
                                      "it ends with .apps.googleusercontent.com")
        new["GOOGLE_CLIENT_SECRET"] = ask("Client secret", env.get("GOOGLE_CLIENT_SECRET", ""), hidden=True, allow_empty=False)
    elif https:
        new.update(GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="")

    print("\n4. \"Continue with Microsoft\"")
    if not https:
        print("   Needs an https:// address (Microsoft refuses http except localhost) -- skipped for now.")
    if https and yes("Set up Microsoft sign-in?", bool(env.get("MS_CLIENT_ID"))):
        print("   In https://entra.microsoft.com : App registrations > New registration. Redirect URI, platform \"Web\":\n"
              f"      {base}/api/auth/oidc/microsoft/callback\n"
              "   Then Certificates & secrets > New client secret, and copy its Value (not its ID).")
        guid = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
        new["MS_CLIENT_ID"] = ask("Application (client) ID", env.get("MS_CLIENT_ID", ""), allow_empty=False,
                                  check=lambda v: "" if guid.match(v) else "a GUID like 1b2c3d4e-...")
        new["MS_CLIENT_SECRET"] = ask("Client secret Value", env.get("MS_CLIENT_SECRET", ""), hidden=True, allow_empty=False)
        new["MS_TENANT"] = ask("Who may sign in: your Directory (tenant) ID for PUC accounts only, "
                               "'organizations' or 'common'", env.get("MS_TENANT", ""), allow_empty=False,
                               check=lambda v: "" if guid.match(v) or v in ("organizations", "common", "consumers")
                               else "a tenant ID (GUID), organizations or common")
    elif https:
        new.update(MS_CLIENT_ID="", MS_CLIENT_SECRET="")

    if not env.get("APP_SECRET"):
        secret_file = HERE / ".secret"  # keep the key the app already made, so nobody is signed out
        new["APP_SECRET"] = secret_file.read_text().strip() if secret_file.exists() else secrets.token_hex(32)

    write_env(new)
    print(f"\nSaved to {ENV}" + (" (the old one is .env.bak)" if ENV.with_name(".env.bak").exists() else "")
          + ". Only you can read it (chmod 600).")
    return new


# --- checking -----------------------------------------------------------------------

def client_check(key: str, p: dict):
    """(True / False / None, text): whether the provider accepts the client ID and secret,
    asked without anyone signing in. None when its answer does not tell."""
    import httpx
    title = p["title"]
    if key == "google":  # a made-up refresh token: "invalid_grant" means the client was accepted
        data = {"grant_type": "refresh_token", "refresh_token": "setup-check"}
    elif re.match(r"^[0-9a-fA-F-]{36}$", p["tenant"]):
        # an app token for your own tenant (no user, no permissions needed)
        data = {"grant_type": "client_credentials", "scope": "https://graph.microsoft.com/.default"}
    else:
        return None, (f"{title}: the keys can only be checked with MS_TENANT set to your tenant ID; "
                      "try \"Continue with Microsoft\" once the app is running")
    try:
        r = httpx.post(p["token"], data={**data, "client_id": p["client_id"], "client_secret": p["secret"]}, timeout=20)
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return False, f"{title}: could not reach {p['token']} ({e.__class__.__name__})"
    err, desc = body.get("error", ""), (body.get("error_description") or "").strip().splitlines()
    desc = desc[0][:160] if desc else ""
    codes = body.get("error_codes") or []
    if r.status_code == 200 or (key == "google" and err == "invalid_grant"):
        return True, f"{title}: client ID and secret accepted. Register this redirect URI: {redirect(key)}"
    if err in ("invalid_client", "unauthorized_client") or set(codes) & {7000215, 7000222, 700016, 90002}:
        what = {7000215: "the client secret is wrong (copy its Value, not its ID)", 7000222: "the client secret has expired",
                700016: "no app with this client ID in that tenant", 90002: "no tenant with this ID"}
        reason = next((what[c] for c in codes if c in what), "the client ID or secret is wrong")
        return False, f"{title}: {reason} ({desc or err})"
    return None, f"{title}: could not tell from the answer ({r.status_code} {err}: {desc}); try a sign-in once the app runs"


def redirect(key: str) -> str:
    import auth
    return auth.redirect_uri(key)


def checks(test_to: str = "") -> bool:
    for k, v in read_env().items():  # .env wins here: this checks the file
        os.environ[k] = v
    import auth
    good = True

    def line(ok, text):
        nonlocal good
        good &= ok is not False
        print(f"  {'ok  ' if ok else 'FAIL' if ok is False else '--  '} {text}")

    print("\nChecks")
    base = auth.base_url()
    line(bool(base) or None, f"APP_BASE_URL {base}" if base else "APP_BASE_URL not set: no Forgot password, no Google / Microsoft")
    if auth.mail_configured():
        to = test_to or os.environ.get("SMTP_USER") or os.environ.get("SMTP_FROM", "")
        try:
            auth.send_mail(to, "PUC Analytics: sign-in setup works",
                           f"E-mail from PUC Analytics works.\n\n{base or ''}\n",
                           auth._mail_html("E-mail works", ["Invitations and password resets can now be e-mailed."],
                                           "Open PUC Analytics", (base or "http://localhost") + "/#/", "Sent by setup_signin.py."))
            line(True, f"e-mail: a test message was sent to {to} (check the inbox and the spam folder)")
        except (RuntimeError, ValueError) as e:
            line(False, f"e-mail: {e}")
    else:
        line(None, "e-mail not set up: the administrator copies invitation links by hand")
    for key, p in auth.providers().items():
        if key not in auth.sso_ready():
            line(False, f"{p['title']}: keys are set but APP_BASE_URL is missing")
            continue
        line(*client_check(key, p))
        if not base.startswith("https://") and urlparse(base).hostname != "localhost":
            line(False, f"{p['title']}: needs an https:// APP_BASE_URL")
    if not auth.providers():
        line(None, "Google / Microsoft sign-in not set up")
    print("\n" + ("All set. Restart the web app to use the new settings." if good else
                  "Fix the lines marked FAIL (run this again), then restart the web app."))
    return good


if __name__ == "__main__":
    try:
        if "--check" in sys.argv:
            sys.exit(0 if checks() else 1)
        wizard()
        checks()
    except (KeyboardInterrupt, EOFError):
        print("\nStopped; .env is unchanged unless it said Saved.")
        sys.exit(1)
