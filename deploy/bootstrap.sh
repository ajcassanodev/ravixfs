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
apt-get install -y -qq git python3 python3-venv sqlite3 ufw unattended-upgrades curl sudo debian-keyring debian-archive-keyring apt-transport-https > /dev/null

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

echo "==> env file (session secrets for cross-subdomain sign-in)"
for var in SESSION_SECRET_STAGING SESSION_SECRET_PROD; do
  if ! grep -q "^$var=" "$ENV_FILE" 2>/dev/null; then
    printf '%s=%s\n' "$var" "$(openssl rand -hex 32)" >> "$ENV_FILE"
    echo "(generated $var)"
  else
    echo "($var exists, keeping)"
  fi
done
chmod 600 "$ENV_FILE"

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

echo "==> staging instance (staging.ravixfs.com)"
STAGING_DIR="/opt/ravixfs-staging"
mkdir -p "$STAGING_DIR"
chown "$APP_USER:$APP_USER" "$STAGING_DIR"
if [ -d "$STAGING_DIR/.git" ]; then
  sudo -u "$APP_USER" git -C "$STAGING_DIR" fetch --quiet origin || true
else
  sudo -u "$APP_USER" git clone --quiet "$REPO_URL" "$STAGING_DIR" || true
fi
# Track the remote staging branch if it exists; otherwise stay on main for now.
if sudo -u "$APP_USER" git -C "$STAGING_DIR" show-ref --verify --quiet refs/remotes/origin/staging 2>/dev/null; then
  sudo -u "$APP_USER" git -C "$STAGING_DIR" checkout -B staging --track origin/staging --quiet 2>/dev/null \
    || sudo -u "$APP_USER" git -C "$STAGING_DIR" checkout --quiet staging
  sudo -u "$APP_USER" git -C "$STAGING_DIR" merge --ff-only --quiet origin/staging || true
  echo "(staging checkout now on origin/staging)"
else
  echo "(remote staging branch not found yet; staging instance mirrors main for now)"
fi
chown -R "$APP_USER:$APP_USER" "$STAGING_DIR"

echo "==> staging python venv"
if [ ! -d "$STAGING_DIR/venv" ]; then
  sudo -u "$APP_USER" python3 -m venv "$STAGING_DIR/venv"
fi
sudo -u "$APP_USER" "$STAGING_DIR/venv/bin/pip" install -q -r "$STAGING_DIR/requirements.txt"

echo "==> staging database"
sudo -u "$APP_USER" RAVIXFS_DB="$STAGING_DIR/ravixfs.db" "$STAGING_DIR/venv/bin/python" \
  -c "import sys; sys.path.insert(0, '$STAGING_DIR'); from app.db import init_db; init_db()"

echo "==> staging systemd units"
for unit in ravixfs-staging.service ravixfs-staging-poll.service ravixfs-staging-poll.timer ravixfs-staging-sync.service ravixfs-staging-sync.timer; do
  cp "$APP_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now ravixfs-staging.service > /dev/null
systemctl enable --now ravixfs-staging-poll.timer > /dev/null
systemctl enable --now ravixfs-staging-sync.timer > /dev/null

echo "==> pool staging instance (staging-footballpool.ravixfs.com)"
POOL_STAGING_DIR="/opt/ravixfs-pool-staging"
SHARED_STAGING_DIR="/opt/ravixfs-shared-staging"
mkdir -p "$POOL_STAGING_DIR" "$SHARED_STAGING_DIR"
chown "$APP_USER:$APP_USER" "$POOL_STAGING_DIR" "$SHARED_STAGING_DIR"
if [ -d "$POOL_STAGING_DIR/.git" ]; then
  sudo -u "$APP_USER" git -C "$POOL_STAGING_DIR" fetch --quiet origin || true
else
  sudo -u "$APP_USER" git clone --quiet "$REPO_URL" "$POOL_STAGING_DIR" || true
fi
if sudo -u "$APP_USER" git -C "$POOL_STAGING_DIR" show-ref --verify --quiet refs/remotes/origin/staging-pool 2>/dev/null; then
  sudo -u "$APP_USER" git -C "$POOL_STAGING_DIR" checkout -B staging-pool --track origin/staging-pool --quiet 2>/dev/null \
    || sudo -u "$APP_USER" git -C "$POOL_STAGING_DIR" checkout --quiet staging-pool
  sudo -u "$APP_USER" git -C "$POOL_STAGING_DIR" merge --ff-only --quiet origin/staging-pool || true
  echo "(pool staging checkout now on origin/staging-pool)"
else
  echo "(remote staging-pool branch not found yet; push it first, then re-run)"
fi
chown -R "$APP_USER:$APP_USER" "$POOL_STAGING_DIR"

echo "==> pool staging python venv"
if [ ! -d "$POOL_STAGING_DIR/venv" ]; then
  sudo -u "$APP_USER" python3 -m venv "$POOL_STAGING_DIR/venv"
fi
sudo -u "$APP_USER" "$POOL_STAGING_DIR/venv/bin/pip" install -q -r "$POOL_STAGING_DIR/requirements.txt"

echo "==> pool staging database + shared member store"
sudo -u "$APP_USER" POOL_DB="$POOL_STAGING_DIR/pool.db" "$POOL_STAGING_DIR/venv/bin/python" \
  -c "import sys; sys.path.insert(0, '$POOL_STAGING_DIR'); from pool.db import init_db; init_db()"
sudo -u "$APP_USER" SHARED_MEMBER_DB="$SHARED_STAGING_DIR/shared.db" "$POOL_STAGING_DIR/venv/bin/python" \
  -c "import sys; sys.path.insert(0, '$POOL_STAGING_DIR'); from shared.identity import init_shared_db; init_shared_db()"

echo "==> pool staging systemd units"
for unit in ravixfs-pool-staging.service ravixfs-pool-staging-sync.service ravixfs-pool-staging-sync.timer ravixfs-pool-poll-staging.service ravixfs-pool-poll-staging.timer; do
  cp "$POOL_STAGING_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now ravixfs-pool-staging.service > /dev/null
systemctl enable --now ravixfs-pool-poll-staging.timer > /dev/null
systemctl enable --now ravixfs-pool-staging-sync.timer > /dev/null

echo "==> initial pool data pull (background)"
systemctl start ravixfs-pool-poll-staging.service || true

echo "==> backups"
chmod +x "$APP_DIR/deploy/backup.sh"
mkdir -p /opt/ravixfs/backups
chown "$APP_USER:$APP_USER" /opt/ravixfs/backups
cp "$APP_DIR/deploy/ravixfs-backup.service" "$APP_DIR/deploy/ravixfs-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ravixfs-backup.timer > /dev/null

echo "==> sudoers (let the app user restart its own services after code sync)"
printf '%s\n' "$APP_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart ravixfs.service, /bin/systemctl restart ravixfs-staging.service, /bin/systemctl restart ravixfs-pool-staging.service" > /etc/sudoers.d/ravixfs
chmod 440 /etc/sudoers.d/ravixfs
visudo -c -q

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

echo "==> ssh hardening (key-only)"
# CRITICAL SAFETY RULE: only disable password auth if a root SSH key exists.
# Otherwise we would lock the owner out of the server.
if [ -s /root/.ssh/authorized_keys ]; then
  mkdir -p /etc/ssh/sshd_config.d
  printf 'PasswordAuthentication no\nPermitRootLogin prohibit-password\n' > /etc/ssh/sshd_config.d/99-ravixfs.conf
  chmod 644 /etc/ssh/sshd_config.d/99-ravixfs.conf
  if sshd -t; then
    systemctl restart sshd
    echo "(password SSH disabled; key-only from now on)"
  else
    echo "sshd config test FAILED; leaving password auth as-is" >&2
    rm -f /etc/ssh/sshd_config.d/99-ravixfs.conf
  fi
else
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo "WARNING: /root/.ssh/authorized_keys is missing or empty."
  echo "Skipping SSH hardening so you don't get locked out."
  echo "Add an SSH key for root, then re-run this script."
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
fi

echo
echo "DONE. Next steps:"
echo "  1. Point ravixfs.com A record at this server's IP."
echo "  2. Open https://ravixfs.com (Caddy fetches the cert automatically)."
echo "  3. Admin pages live at /admin/<token> (token printed above on first run)."
