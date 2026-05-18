#!/bin/bash
# deploy-staging.sh — Deploy StrongPass to STAGING on the same Hetzner VPS
#
# STAGING ONLY — does NOT touch /opt/strongpass/current (production).
#
# Usage (run from your local machine):
#   ssh root@strongpass.live "bash -s" < infra/scripts/deploy-staging.sh
#
# Or with a GitHub token for private repos:
#   GH_TOKEN=ghp_xxx ssh root@strongpass.live "GH_TOKEN=ghp_xxx bash -s" < infra/scripts/deploy-staging.sh
#
# Staging URL (after DNS + SSL are set up):
#   https://staging.strongpass.live
#
# What this script does:
#   1. Creates /opt/strongpass/staging/ (never touches /current/)
#   2. Clones or pulls the latest main branch from GitHub
#   3. Creates a Python venv and installs requirements
#   4. Writes a staging gunicorn config (port 8081)
#   5. Installs the strongpass-staging systemd service
#   6. Installs the nginx staging vhost config (HTTP only until SSL step)
#   7. Starts/restarts the staging service
#   8. Prints staging status + access URL

set -euo pipefail

STAGING_DIR="/opt/strongpass/staging"
STAGING_PORT="8081"
GITHUB_REPO="https://github.com/anthony89f-creator/Strongpass-live.git"
SERVICE_NAME="strongpass-staging"
NGINX_CONF_SRC="$STAGING_DIR/infra/nginx/staging.conf"
NGINX_CONF_DST="/etc/nginx/sites-available/strongpass-staging"
SYSTEMD_SRC="$STAGING_DIR/infra/systemd/strongpass-staging.service"
SYSTEMD_DST="/etc/systemd/system/strongpass-staging.service"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   StrongPass — STAGING DEPLOY                       ║"
echo "║   Target: $STAGING_DIR            ║"
echo "║   Port:   $STAGING_PORT (production = 8080)              ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── Safety check: confirm we are NOT in the production directory ───────────────
if [ "$PWD" = "/opt/strongpass/current" ]; then
    echo "ERROR: refusing to run from production directory"
    exit 1
fi

# ── Step 1: Clone or pull ──────────────────────────────────────────────────────
echo "→ Step 1: Clone / pull from GitHub"
if [ -d "$STAGING_DIR/.git" ]; then
    echo "  existing repo found — pulling main"
    git -C "$STAGING_DIR" fetch origin
    git -C "$STAGING_DIR" reset --hard origin/main
    git -C "$STAGING_DIR" clean -fd
else
    echo "  fresh clone into $STAGING_DIR"
    mkdir -p "$(dirname $STAGING_DIR)"
    CLONE_URL="$GITHUB_REPO"
    if [ -n "${GH_TOKEN:-}" ]; then
        CLONE_URL="https://$GH_TOKEN@github.com/anthony89f-creator/Strongpass-live.git"
    fi
    git clone --depth=1 --branch main "$CLONE_URL" "$STAGING_DIR"
fi
echo "  ✓ code at: $STAGING_DIR"
echo "  ✓ HEAD: $(git -C $STAGING_DIR log --oneline -1)"

# ── Step 2: Python venv ────────────────────────────────────────────────────────
echo ""
echo "→ Step 2: Python venv + dependencies"
if [ ! -d "$STAGING_DIR/venv" ]; then
    python3 -m venv "$STAGING_DIR/venv"
fi
"$STAGING_DIR/venv/bin/pip" install --quiet --upgrade pip
"$STAGING_DIR/venv/bin/pip" install --quiet -r "$STAGING_DIR/requirements.txt"
echo "  ✓ venv ready"

# ── Step 3: Staging gunicorn config ───────────────────────────────────────────
echo ""
echo "→ Step 3: Gunicorn staging config (port $STAGING_PORT)"
cat > "$STAGING_DIR/gunicorn-staging.conf.py" << GCFG
# Gunicorn config for staging — port $STAGING_PORT
bind         = "127.0.0.1:$STAGING_PORT"
workers      = 1
threads      = 8
worker_class = "gthread"
timeout      = 3600
keepalive    = 5
accesslog    = "-"
errorlog     = "-"
loglevel     = "info"
GCFG
echo "  ✓ gunicorn-staging.conf.py written"

# ── Step 4: Systemd service ────────────────────────────────────────────────────
echo ""
echo "→ Step 4: Systemd service ($SERVICE_NAME)"
cp "$SYSTEMD_SRC" "$SYSTEMD_DST"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
echo "  ✓ $SERVICE_NAME installed and enabled"

# ── Step 5: Start / restart staging service ───────────────────────────────────
echo ""
echo "→ Step 5: Start staging service"
if systemctl is-active --quiet "$SERVICE_NAME"; then
    systemctl restart "$SERVICE_NAME"
    echo "  ✓ restarted $SERVICE_NAME"
else
    systemctl start "$SERVICE_NAME"
    echo "  ✓ started $SERVICE_NAME"
fi
sleep 2
systemctl is-active --quiet "$SERVICE_NAME" && echo "  ✓ service is RUNNING" || echo "  ✗ service FAILED — check: journalctl -u $SERVICE_NAME -n 50"

# ── Step 6: Nginx vhost (HTTP only — SSL added manually) ──────────────────────
echo ""
echo "→ Step 6: Nginx staging vhost (HTTP, port 80 only until SSL cert added)"

# Write an HTTP-only nginx block for immediate testing (no SSL yet)
cat > "$NGINX_CONF_DST" << NCFG
# StrongPass staging — HTTP only
# To add SSL: certbot --nginx -d staging.strongpass.live --email your@email.com --agree-tos -n
server {
    listen 80;
    server_name staging.strongpass.live;

    location /stream {
        proxy_pass         http://127.0.0.1:$STAGING_PORT;
        proxy_http_version 1.1;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
        chunked_transfer_encoding on;
    }

    location / {
        proxy_pass         http://127.0.0.1:$STAGING_PORT;
        proxy_http_version 1.1;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_read_timeout 60s;
        client_max_body_size 10M;
    }
}
NCFG

ln -sf "$NGINX_CONF_DST" /etc/nginx/sites-enabled/strongpass-staging 2>/dev/null || true
nginx -t && systemctl reload nginx && echo "  ✓ nginx reloaded with staging vhost"

# ── Step 7: Quick health check ─────────────────────────────────────────────────
echo ""
echo "→ Step 7: Health check (127.0.0.1:$STAGING_PORT)"
sleep 1
HEALTH=$(curl -sf "http://127.0.0.1:$STAGING_PORT/health" 2>/dev/null || echo "FAIL")
echo "  health: $HEALTH"

# ── Done ──────────────────────────────────────────────────────────────────────
PROD_HEAD=$(git -C /opt/strongpass/current log --oneline -1 2>/dev/null || echo "unknown")
STAGING_HEAD=$(git -C "$STAGING_DIR" log --oneline -1)
SERVER_IP=$(curl -sf https://icanhazip.com 2>/dev/null || echo "unknown")

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║   STAGING DEPLOY COMPLETE                               ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║                                                          ║"
echo "║  Staging URL (after DNS record):                        ║"
echo "║    http://staging.strongpass.live                       ║"
echo "║                                                          ║"
echo "║  Direct (no DNS needed):                                ║"
echo "║    http://$SERVER_IP:$STAGING_PORT                                 ║"
echo "║                                                          ║"
echo "║  Staging build:    $STAGING_HEAD   ║"
echo "║  Production build: $PROD_HEAD   ║"
echo "║                                                          ║"
echo "║  COMP_PASSWORD for staging: staging                     ║"
echo "║                                                          ║"
echo "║  To add SSL:                                            ║"
echo "║    certbot --nginx -d staging.strongpass.live \\        ║"
echo "║      --email your@email.com --agree-tos -n              ║"
echo "║                                                          ║"
echo "║  To view logs:                                          ║"
echo "║    journalctl -u strongpass-staging -f                  ║"
echo "║                                                          ║"
echo "║  To update staging later:                               ║"
echo "║    ssh root@strongpass.live \"bash -s\" < infra/scripts/deploy-staging.sh  ║"
echo "║                                                          ║"
echo "╚══════════════════════════════════════════════════════════╝"
