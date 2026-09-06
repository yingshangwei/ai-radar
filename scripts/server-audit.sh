#!/usr/bin/env bash
set -eu
# Run on the target host before deploying. This intentionally prints no environment or config secrets.
uname -a
id
ss -ltnp
systemctl list-units --type=service --state=running --no-pager
df -h /
free -m
command -v docker nginx caddy node python3 codex || true
if command -v docker >/dev/null; then
    docker ps --format '{{.Names}} {{.Ports}} {{.Image}}'
fi
ls -ld /opt/ai-radar /etc/ai-radar /var/lib/ai-radar /etc/nginx/sites-enabled /etc/caddy 2>/dev/null || true
