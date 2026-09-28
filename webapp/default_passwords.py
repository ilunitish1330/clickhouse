#!/usr/bin/env python3
"""Lists active accounts whose password is still the first-start default (Puc@2026, or
APP_DEFAULT_PASSWORD), and can switch them off. Run it before putting the app on the internet:
anyone could sign in to such an account.

    python3 webapp/default_passwords.py             # list them
    python3 webapp/default_passwords.py --disable   # also untick "Account active" for each

A disabled account keeps its roles; an administrator turns it back on in Users & roles (Edit)
and gives it a new password.
"""
import os
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]

import server  # noqa: E402  (.env, the users store)


def on_default() -> list[dict]:
    pw = os.environ.get("APP_DEFAULT_PASSWORD", "Puc@2026")
    users = server.q(f"SELECT * FROM {server.APP_DB}.users FINAL WHERE active = 1 ORDER BY username")
    return [u for u in users if server.password_ok(u, pw)]


def main() -> int:
    try:
        found = on_default()
    except RuntimeError as e:
        print(f"Could not read the users: {str(e).splitlines()[0]}")
        return 2
    if not found:
        print("No active account uses the default password.")
        return 0
    print(f"{len(found)} active account(s) still use the default password:")
    for u in found:
        print(f"  {u['username']:<18} {u['full_name']}  ({u['role']})")
    if "--disable" not in sys.argv:
        print("\nGive each a new password in Users & roles (Edit), or run this with --disable.")
        return 1
    for u in found:
        if "admin" in server.role_ids(u["role"]):
            print(f"  kept {u['username']} active (an administrator): change its password now in the app")
            continue
        server.save_user(u["username"], u["full_name"], u["role"], u["region_code"], must_change=u["must_change"],
                         active=0, keep=u)
        server.audit("setup", "user_disabled", f"{u['username']} (default password)")
        print(f"  disabled {u['username']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
