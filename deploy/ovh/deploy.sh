#!/usr/bin/env bash
# Build + (re)deploy the whole stack. Run after bootstrap.sh and after editing
# .env.production. Safe to re-run for every update (git pull && ./deploy.sh).
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env.production ] || { echo "missing .env.production — run ./bootstrap.sh first"; exit 1; }
[ -f db/certs/server.crt ] || { echo "missing db/certs/server.crt — run ./bootstrap.sh first"; exit 1; }

# Load DOMAIN etc. for the frontend build + compose interpolation.
set -a; . ./.env.production; set +a

echo "==> Building the real frontend (VITE_DEMO unset → talks to the live API)"
( cd ../../web && npm ci && VITE_DEMO= npm run build )

echo "==> Building images + starting the stack"
docker compose --env-file .env.production up -d --build

echo "==> Waiting for API health"
for i in $(seq 1 40); do
  if docker compose --env-file .env.production exec -T app \
       python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8090/api/health').status==200 else 1)" >/dev/null 2>&1; then
    echo "    API healthy."
    break
  fi
  sleep 2
done

docker compose --env-file .env.production ps
echo "==> Deployed. Visit https://${DOMAIN}"
echo "    First org/user:  docker compose --env-file .env.production exec app /app/admin create-org --name \"Acme PM\""
echo "                     docker compose --env-file .env.production exec app /app/admin create-user --org-id 1 --email you@acme.com --password <temp>"
