#!/bin/bash
# EC2 user data for the Polymarket PAPER bot (Ubuntu 24.04 LTS, arm64, t4g.small).
# Paste this whole file into "Advanced details -> User data" when launching the instance.
# Before pasting, fill in the two values marked FILL IN below (in a text editor, never in chat).
# The instance only reads public market data and writes to your database; it places no real orders.
set -euo pipefail
exec > >(tee -a /var/log/polybot-setup.log) 2>&1

# ============================ FILL IN ============================
DATABASE_URL='FILL_IN_SUPABASE_SESSION_POOLER_URI_WITH_?sslmode=require'
STATUS_GH_TOKEN='FILL_IN_GITHUB_FINE_GRAINED_TOKEN'   # contents read/write on Phaviwylera/polymarketbot only
# =================================================================

REPO_URL='https://github.com/Phaviwylera/polymarketbot.git'
APP=/opt/polybot; DATA=/var/lib/polybot

# 2 GB swap: package builds and the research trainer need headroom on a 2 GB instance.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip git ca-certificates
useradd --system --create-home --home-dir "$APP" --shell /usr/sbin/nologin polybot 2>/dev/null || true
mkdir -p "$DATA" && chown polybot:polybot "$DATA"

if [ ! -d "$APP/repo/.git" ]; then sudo -u polybot git clone --depth 50 "$REPO_URL" "$APP/repo"; fi
sudo -u polybot python3 -m venv "$APP/venv"
sudo -u polybot "$APP/venv/bin/pip" install -q --upgrade pip
sudo -u polybot "$APP/venv/bin/pip" install -q -r "$APP/repo/arb-lab-v4/requirements.txt" pytest
# Optional research model (xgboost); the bot runs without it.
sudo -u polybot "$APP/venv/bin/pip" install -q -r "$APP/repo/arb-lab-v4/requirements-research.txt" || echo 'research extras skipped'

# Secrets live only in this root-readable file (mode 600), never in the repository.
umask 077
cat > /etc/polybot.env <<ENV
POLY_DATABASE_URL=${DATABASE_URL}
POLY_STATUS_GH_TOKEN=${STATUS_GH_TOKEN}
POLY_STATUS_REPO=Phaviwylera/polymarketbot
POLY_STATUS_BRANCH=bot-status
POLY_HOST_LABEL=aws-ec2-t4g.small
POLY_DATA_DIR=${DATA}
POLY_FRESH_START_AT=1792782893
POLY_PAUSED_LANES=
POLY_RESEARCH=1
POLY_WALLET_STUDY=1
PYTHONUNBUFFERED=1
ENV
chmod 600 /etc/polybot.env
umask 022

cat > /etc/systemd/system/polybot.service <<'UNIT'
[Unit]
Description=Polymarket paper bot (paper only)
After=network-online.target
Wants=network-online.target

[Service]
User=polybot
WorkingDirectory=/opt/polybot/repo/arb-lab-v4
EnvironmentFile=/etc/polybot.env
ExecStart=/opt/polybot/venv/bin/uvicorn server:app --host 0.0.0.0 --port 80 --no-access-log
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=5
TimeoutStopSec=30
MemoryMax=1800M
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

# Auto-update: every 5 minutes pull main; restart only if the full test suite passes on the new code.
cat > /usr/local/bin/polybot-update <<'UPD'
#!/bin/bash
# Every git command runs as the repo owner (polybot): git refuses root in a repo owned by another user
# ("dubious ownership"), which silently turned every update check into a no-op.
set -uo pipefail
cd /opt/polybot/repo || exit 1
G="sudo -u polybot git"
old=$($G rev-parse HEAD) || { echo "$(date -u +%FT%TZ) git rev-parse failed" >> /var/log/polybot-update.log; exit 1; }
$G fetch -q origin main || { echo "$(date -u +%FT%TZ) git fetch failed" >> /var/log/polybot-update.log; exit 0; }
new=$($G rev-parse origin/main)
[ -n "$new" ] && [ "$old" = "$new" ] && exit 0
$G reset -q --hard "$new"
if ! $G diff --quiet "$old" "$new" -- arb-lab-v4/requirements.txt; then
  sudo -u polybot /opt/polybot/venv/bin/pip install -q -r arb-lab-v4/requirements.txt
fi
cd arb-lab-v4
if sudo -u polybot timeout 600 /opt/polybot/venv/bin/python -m pytest -q -x >/var/log/polybot-update-tests.log 2>&1; then
  echo "$(date -u +%FT%TZ) update ${old:0:7} -> ${new:0:7}: tests passed, restarting" >> /var/log/polybot-update.log
  systemctl restart polybot
else
  echo "$(date -u +%FT%TZ) update ${new:0:7} FAILED tests, staying on ${old:0:7}" >> /var/log/polybot-update.log
  cd .. && $G reset -q --hard "$old"
fi
UPD
chmod 755 /usr/local/bin/polybot-update

cat > /etc/systemd/system/polybot-update.service <<'UNIT'
[Unit]
Description=Pull and test the paper bot, restart if tests pass
[Service]
Type=oneshot
ExecStart=/usr/local/bin/polybot-update
UNIT
cat > /etc/systemd/system/polybot-update.timer <<'UNIT'
[Unit]
Description=Check for paper bot updates every 5 minutes
[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
UNIT

# Keep logs bounded on an 8-20 GB disk.
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=300M\n' > /etc/systemd/journald.conf.d/polybot.conf
systemctl restart systemd-journald

systemctl daemon-reload
systemctl enable --now polybot.service polybot-update.timer
echo "polybot setup finished $(date -u +%FT%TZ)"
