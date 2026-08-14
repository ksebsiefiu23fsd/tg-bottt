#!/usr/bin/env bash
set -euo pipefail

cd /opt/tg-bot
git pull --ff-only origin main
.venv/bin/pip install --no-cache-dir -r requirements.txt
install -m 0644 deploy/tg-bot.service /etc/systemd/system/tg-bot.service
systemctl daemon-reload
systemctl enable --now tg-bot.service
systemctl restart tg-bot.service
systemctl --no-pager --full status tg-bot.service
