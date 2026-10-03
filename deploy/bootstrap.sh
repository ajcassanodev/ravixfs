#!/bin/bash
# ravixfs one-shot server setup for a fresh Ubuntu 24.04 box (Hetzner CX22).
# Idempotent: safe to re-run. Run from the Hetzner web console as root:
#   curl -fsSL https://raw.githubusercontent.com/ajcassanodev/ravixfs/main/deploy/bootstrap.sh | bash
set -euo pipefail

REPO_URL="https://github.com/ajcassanodev/ravixfs.git"
APP_DIR="/opt/ravixfs"
APP_USER="ravixfs"
ENV_FILE="/etc/ravixfs.env"

echo "==> apt update + base packages"
apt-get update -qq
apt-get install -y -qq git python3 python3-venv sqlite3 ufw unattended-upgrades curl debian-keyring debian-archive-keyring apt-transport-https > /dev/null

echo "==> caddy (official repo)"
if [ ! -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    | tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
  apt-get update -qq
fi
apt-get install -y -qq caddy > /dev/null

echo "==> app user"
id -u "$APP_USER" > /dev/null 2>&1 || useradd -r -m -s /bin/bash "$APP_USER"
mkdir -p "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> code"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only --quiet || true
else
  sudo -u "$APP_USER" git clone --quiet "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> python venv"
if [ ! -d "$APP_DIR/venv" ]; then
  sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> env file (admin token)"
if [ ! -f "$ENV_FILE" ]; then
  ADMIN_TOKEN="$(openssl rand -hex 24)"
  printf 'RAVIXFS_DB=%s/ravixfs.db\nADMIN_TOKEN=%s\n' "$APP_DIR" "$ADMIN_TOKEN" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "ADMIN TOKEN (save this! it is the /admin/<token> URL key): $ADMIN_TOKEN"
else
  echo "(env file exists, keeping existing admin token)"
fi

echo "==> database"
sudo -u "$APP_USER" RAVIXFS_DB="$APP_DIR/ravixfs.db" "$APP_DIR/venv/bin/python" \
  -c "import sys; sys.path.insert(0, '$APP_DIR'); from app.db import init_db; init_db()"

echo "==> systemd units"
for unit in ravixfs.service ravixfs-poll.service ravixfs-poll.timer ravixfs-sync.service ravixfs-sync.timer; do
  cp "$APP_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now ravixfs.service > /dev/null
systemctl enable --now ravixfs-poll.timer > /dev/null
systemctl enable --now ravixfs-sync.timer > /dev/null

echo "==> caddy"
cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak 2>/dev/null || true
cp "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
systemctl reload caddy

echo "==> firewall"
ufw allow 22/tcp > /dev/null
ufw allow 80/tcp > /dev/null
ufw allow 443/tcp > /dev/null
ufw --force enable > /dev/null

echo "==> unattended upgrades"
echo 'APT::Periodic::Update-Package-Lists "1";' > /etc/apt/apt.conf.d/20auto-upgrades
echo 'APT::Periodic::Unattended-Upgrade "1";' >> /etc/apt/apt.conf.d/20auto-upgrades

echo "==> initial data pull (background)"
systemctl start ravixfs-poll.service || true

echo
echo "DONE. Next steps:"
echo "  1. Point ravixfs.com A record at this server's IP."
echo "  2. Open https://ravixfs.com (Caddy fetches the cert automatically)."
echo "  3. Admin pages live at /admin/<token> (token printed above on first run)."
