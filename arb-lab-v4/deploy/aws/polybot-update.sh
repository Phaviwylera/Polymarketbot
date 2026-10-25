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
