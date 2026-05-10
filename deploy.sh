#!/bin/bash
# deploy.sh — First-time setup for strongpass.live on Hetzner (Ubuntu 22.04)
# Run as root: bash deploy.sh
# Then edit /etc/systemd/system/strongman.service to set COMP_PASSWORD before starting.

set -e

echo "=== Strongman Competition Engine — Hetzner Deploy ==="

# 1. System packages
apt-get update -y
apt-get install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx git

# 2. Create dedicated system user
if ! id -u strongman &>/dev/null; then
    useradd --system --create-home --home-dir /opt/strongman --shell /bin/bash strongman
fi

# 3. Copy files
mkdir -p /opt/strongman
cp -r . /opt/strongman/
chown -R strongman:strongman /opt/strongman

# 4. Python venv + dependencies
sudo -u strongman python3 -m venv /opt/strongman/venv
sudo -u strongman /opt/strongman/venv/bin/pip install -r /opt/strongman/requirements.txt

# 5. Systemd service
cp /opt/strongman/strongman.service /etc/systemd/system/strongman.service
echo ""
echo ">>> IMPORTANT: Edit /etc/systemd/system/strongman.service and set COMP_PASSWORD before starting <<<"
echo ""

systemctl daemon-reload
systemctl enable strongman

# 6. Nginx config
cp /opt/strongman/nginx.conf /etc/nginx/sites-available/strongpass
ln -sf /etc/nginx/sites-available/strongpass /etc/nginx/sites-enabled/strongpass
rm -f /etc/nginx/sites-enabled/default
nginx -t

# 7. SSL via Let's Encrypt
echo ""
echo "=== SSL Setup ==="
echo "Run this command to get your SSL certificate (replace with your email):"
echo "  certbot --nginx -d strongpass.live -d www.strongpass.live --email your@email.com --agree-tos --non-interactive"
echo ""

# 8. Start services
systemctl start strongman
systemctl start nginx

echo ""
echo "=== Deploy complete ==="
echo "  App:     http://localhost:8080"
echo "  Public:  https://strongpass.live"
echo ""
echo "Useful commands:"
echo "  systemctl status strongman       — check app status"
echo "  journalctl -u strongman -f       — live logs"
echo "  systemctl restart strongman      — restart after changes"
echo "  nginx -t && systemctl reload nginx — reload nginx config"
