#!/usr/bin/env bash
# Read-only host inventory. Does not read application secrets or alter services.
set -eu
uname -m
ss -ltnp
systemctl list-units --type=service --state=running --no-pager --no-legend
free -m
df -h /
for name in python3 nginx caddy docker node codex; do command -v "$name" || true; done
if command -v ufw >/dev/null; then ufw status; fi
if [ -e /opt/ai-radar/current ]; then readlink -f /opt/ai-radar/current; fi
curl --silent --max-time 5 -o /dev/null -w 'existing_8080_status=%{http_code}\n' http://127.0.0.1:8080/ || true
