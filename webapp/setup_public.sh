#!/usr/bin/env bash
# Puts PUC Analytics on a public https address, so invitation and password-reset links open
# from anywhere, and makes the app and the tunnel start at boot. Two tunnel services:
#
#   bash webapp/setup_public.sh localxpose <subdomain> <access-token>    # -> https://<subdomain>.loclx.io
#   bash webapp/setup_public.sh ngrok <your-domain>.ngrok-free.app <authtoken>
#
# The address must stay the same (e-mailed links use it), so:
#   LocalXpose: a chosen subdomain needs a paid plan; copy the access token from
#               https://localxpose.io/dashboard/access . REGION=us|eu|ap (default us).
#   ngrok:      the free plan includes one static domain (dashboard > Domains);
#               copy the authtoken from the dashboard.
# Run from the repository folder with the python3 that runs the app. It asks for sudo.
set -euo pipefail

PROVIDER="${1:-}"; NAME="${2:-}"; TOKEN="${3:-}"
PORT="${APP_PORT:-8021}"
REGION="${REGION:-us}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3)"
ME="$(id -un)"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
usage="Usage:  bash webapp/setup_public.sh localxpose <subdomain> <access-token>
        bash webapp/setup_public.sh ngrok <your-domain>.ngrok-free.app <authtoken>"

[[ -n "$PROVIDER" && -n "$NAME" && -n "$TOKEN" ]] || die "$usage"
"$PY" -c "import fastapi, httpx, uvicorn" 2>/dev/null \
  || die "$PY lacks the app's libraries. Run: pip install -r webapp/requirements.txt (or use the python that runs the app)"

# the tunnel's secret goes in a root-only file, not in the (world-readable) service file
TUNNEL_ENV=/etc/puc-tunnel.env

case "$PROVIDER" in
  localxpose|loclx)
    SUB="${NAME,,}"; SUB="${SUB#https://}"; SUB="${SUB%%.loclx.io*}"
    [[ "$SUB" =~ ^[a-z0-9][a-z0-9-]{1,40}$ ]] || die "Subdomain: lower-case letters, digits and dashes, e.g. puc-analytics"
    [[ "$REGION" =~ ^(us|eu|ap)$ ]] || die "REGION must be us, eu or ap"
    DOMAIN="$SUB.loclx.io"
    say "1/6  LocalXpose"
    if ! command -v loclx >/dev/null; then
      case "$(uname -m)" in x86_64) ARCH=amd64;; aarch64|arm64) ARCH=arm64;; armv7l|armv6l) ARCH=arm;; i?86) ARCH=386;;
        *) die "No LocalXpose build for $(uname -m)";; esac
      TMP="$(mktemp -d)"
      curl -fsSL "https://api.localxpose.io/api/downloads/loclx-linux-$ARCH.zip" -o "$TMP/loclx.zip" \
        || die "Could not download LocalXpose (try: sudo snap install localxpose)"
      "$PY" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extract('loclx', sys.argv[2])" "$TMP/loclx.zip" "$TMP"
      sudo install -m 755 "$TMP/loclx" /usr/local/bin/loclx
      rm -rf "$TMP"
    fi
    TUNNEL_BIN="$(command -v loclx)"
    echo "LX_ACCESS_TOKEN=$TOKEN" | sudo tee "$TUNNEL_ENV" >/dev/null
    TUNNEL_CMD="$TUNNEL_BIN tunnel http --to=127.0.0.1:$PORT --region=$REGION --subdomain=$SUB"
    TUNNEL_NAME="LocalXpose"
    echo "LocalXpose is ready; the address will be https://$DOMAIN"
    ;;
  ngrok)
    DOMAIN="${NAME,,}"; DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN%%/*}"
    [[ "$DOMAIN" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]] || die "That does not look like a domain: $DOMAIN"
    say "1/6  ngrok"
    if ! command -v ngrok >/dev/null; then
      curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
      echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list >/dev/null
      sudo apt-get update -qq && sudo apt-get install -y -qq ngrok
    fi
    TUNNEL_BIN="$(command -v ngrok)"
    URLFLAG="--url"; "$TUNNEL_BIN" http --help 2>&1 | grep -q -- "--url" || URLFLAG="--domain"   # older agents
    echo "NGROK_AUTHTOKEN=$TOKEN" | sudo tee "$TUNNEL_ENV" >/dev/null
    TUNNEL_CMD="$TUNNEL_BIN http $URLFLAG=$DOMAIN $PORT --log=stdout --log-level=warn"
    TUNNEL_NAME="ngrok"
    echo "ngrok $("$TUNNEL_BIN" version | awk '{print $3}') is ready"
    ;;
  *) die "$usage" ;;
esac
sudo chmod 600 "$TUNNEL_ENV"

say "2/6  Accounts still on the default password (anyone on the internet could use them)"
rc=0; "$PY" "$REPO/webapp/default_passwords.py" || rc=$?
if [[ $rc == 1 ]]; then
  read -r -p "Switch those accounts off now (administrators are kept, change their password in the app)? [Y/n] " a
  if [[ ! "$a" =~ ^[Nn] ]]; then "$PY" "$REPO/webapp/default_passwords.py" --disable
  else echo "Left as they are: give them new passwords in Users & roles before sharing the address."; fi
elif [[ $rc != 0 ]]; then
  echo "Could not check (is ClickHouse running?). Check Users & roles for \"Must change password\" yourself."
fi

say "3/6  The public address in .env"
"$PY" "$REPO/webapp/setup_signin.py" --base-url "https://$DOMAIN" || true

say "4/6  Stopping an app started by hand on port $PORT"
PIDS="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u || true)"
for pid in $PIDS; do
  if ps -o args= -p "$pid" | grep -q "server.py"; then
    echo "stopping $(ps -o args= -p "$pid") (pid $pid)"; kill "$pid" 2>/dev/null || sudo kill "$pid"
  else
    die "Port $PORT is used by something else: $(ps -o args= -p "$pid"). Choose another: APP_PORT=8022 bash webapp/setup_public.sh ..."
  fi
done
sleep 2

say "5/6  Services that start at boot"
sudo tee /etc/systemd/system/puc-analytics.service >/dev/null <<EOF
[Unit]
Description=PUC Analytics web app
After=network-online.target clickhouse-server.service
Wants=network-online.target

[Service]
User=$ME
WorkingDirectory=$REPO/webapp
Environment=APP_PORT=$PORT
ExecStart=$PY server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl disable --now puc-ngrok puc-tunnel >/dev/null 2>&1 || true   # an earlier tunnel
sudo tee /etc/systemd/system/puc-tunnel.service >/dev/null <<EOF
[Unit]
Description=Public https address for PUC Analytics ($TUNNEL_NAME)
After=network-online.target puc-analytics.service
Wants=network-online.target

[Service]
User=$ME
Environment=HOME=$HOME
EnvironmentFile=$TUNNEL_ENV
ExecStart=$TUNNEL_CMD
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now puc-analytics puc-tunnel >/dev/null 2>&1
sudo systemctl restart puc-analytics puc-tunnel

say "6/6  Checking"
ok_local=""; ok_public=""
for _ in $(seq 30); do
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" && ok_local=1 && break; sleep 1
done
for _ in $(seq 30); do
  curl -fsS -o /dev/null -H "ngrok-skip-browser-warning: 1" "https://$DOMAIN/" && ok_public=1 && break; sleep 2
done
[[ -n "$ok_local" ]] && echo "ok    the app answers on port $PORT" \
  || echo "FAIL  the app does not answer: sudo journalctl -u puc-analytics -n 50"
[[ -n "$ok_public" ]] && echo "ok    https://$DOMAIN opens from the internet" \
  || echo "FAIL  https://$DOMAIN does not answer yet: sudo journalctl -u puc-tunnel -n 50"

cat <<EOF

Done. Open https://$DOMAIN from any phone or computer. Invitation and reset e-mails now link there.
(ngrok's free plan first shows a "You are about to visit" page: click Visit Site.)

  Google sign-in: in Google Cloud Console > Credentials > your OAuth client, set the redirect URI to
      https://$DOMAIN/api/auth/oidc/google/callback
  Status:   systemctl status puc-analytics puc-tunnel
  Logs:     sudo journalctl -u puc-analytics -f
  After a git pull:  sudo systemctl restart puc-analytics
EOF
