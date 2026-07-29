#!/usr/bin/env bash
# One-time OVH VPS preparation. Run as root (or via sudo) on a fresh Ubuntu
# 22.04/24.04 box. Idempotent — safe to re-run.
#
#   ssh root@<vps-ip>
#   git clone <repo> metered && cd metered/deploy/ovh
#   ./bootstrap.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> 1/5 Docker Engine + Compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin missing"; exit 1; }

echo "==> 2/5 Firewall (SSH + HTTP + HTTPS only)"
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp  || true
  ufw allow 80/tcp  || true
  ufw allow 443/tcp || true
  yes | ufw enable   || true
fi

echo "==> 3/5 Swap (headful Chromium is memory-bursty; add 4G if none)"
if ! swapon --show | grep -q .; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> 4/5 Self-signed cert for internal Postgres TLS (enables sslmode=require)"
CERT_DIR="./db/certs"
mkdir -p "$CERT_DIR"
if [ ! -f "$CERT_DIR/server.crt" ]; then
  openssl req -new -x509 -days 3650 -nodes -text \
    -subj "/CN=metered-db" \
    -out "$CERT_DIR/server.crt" -keyout "$CERT_DIR/server.key"
  # Postgres refuses a key that is group/world-readable; official image runs as uid 999.
  chmod 600 "$CERT_DIR/server.key"
  chown 999:999 "$CERT_DIR/server.key" "$CERT_DIR/server.crt" || true
fi

echo "==> 5/5 .env.production"
if [ ! -f .env.production ]; then
  cp .env.production.example .env.production
  echo "    -> created .env.production — EDIT IT NOW (DOMAIN + run the openssl commands inside it), then run ./deploy.sh"
else
  echo "    -> .env.production already exists (left untouched)"
fi

echo "==> bootstrap done."
