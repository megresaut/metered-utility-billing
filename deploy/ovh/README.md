# Deploying Metered to OVH (single VPS)

Gets the **real** product live: React frontend + Go API + in-process scraper +
self-hosted Postgres, all on one box behind Caddy with automatic HTTPS. This is
the cost-lean alternative to the Render blueprint (`../../render.yaml`).

```
Browser ──HTTPS──▶ Caddy ──┬─ /api/* ─▶ app (Go API + worker + scheduler + Chromium)
   (one domain)            └─ /*    ─▶ built React SPA (static)
                                          app ──▶ Postgres 16 (internal TLS, RLS-enforcing role)
                                          app ──▶ /data volume (PDFs, 2FA handoff)
```

**Compartmentalization note:** this is the monolithic "get live" topology (API +
worker in one process). Splitting the scraper fleet out and adding residential
proxies is **Track D (#23)** — do it once real traffic justifies it.

---

## What you need first

1. An **OVH VPS** — Ubuntu 22.04/24.04, **≥ 4 GB RAM** (headful Chromium is
   memory-hungry; 8 GB if you'll run several providers at once).
2. A **domain** you control, with a DNS **A record** pointing at the VPS IP
   (e.g. `app.yourdomain.com → <vps-ip>`). Caddy needs this resolving before it
   can issue a TLS cert.
3. An **OpenRouter API key** (AI bill extraction).

## Steps

```bash
# on the VPS, as root
git clone <this-repo> metered
cd metered/deploy/ovh

# 1. one-time prep: docker, firewall, swap, internal DB cert, .env scaffold
./bootstrap.sh

# 2. fill in secrets + domain
nano .env.production      # set DOMAIN; run the `openssl rand` commands it lists
#   POSTGRES_SUPER_PASSWORD  openssl rand -hex 24
#   APP_DB_PASSWORD          openssl rand -hex 24
#   JWT_SECRET               openssl rand -hex 32
#   CRED_MASTER_KEY          openssl rand -hex 32   (exactly 64 hex chars — BACK IT UP)
#   OPENROUTER_API_KEY       your key

# 3. build frontend + images, start everything (migrations run on boot)
./deploy.sh
```

Then create the first org/user (no self-serve signup yet — that's Track B):

```bash
docker compose --env-file .env.production exec app /app/admin create-org  --name "Acme Property Management"
docker compose --env-file .env.production exec app /app/admin create-user --org-id 1 --email admin@acme.com --password <temp>
```

Visit `https://<DOMAIN>` and log in. Health: `curl https://<DOMAIN>/api/health`.

## Updating

```bash
git pull && ./deploy.sh
```

## Backups (issue #9)

```bash
crontab -e
# 0 3 * * *  /root/metered/deploy/ovh/backup.sh >> /var/log/metered-backup.log 2>&1
```
`backup.sh` keeps 14 daily dumps **on the box** — copy them OFF-box (object
storage) and test a restore before relying on them.

## Why it's set up this way

- **Non-superuser DB role (`metered_app`)** — RLS (`003_rls.sql`) only enforces
  for non-superusers. The `01-app-role.sh` init creates it; the app connects as
  it. Do **not** point `DATABASE_URL` at the `metered` superuser.
- **Internal Postgres TLS** — production config rejects `sslmode=disable`, so the
  db runs with a self-signed cert and the app uses `sslmode=require` (encrypts
  the in-host hop; no CA verification needed).
- **Same-origin** — Caddy serves the SPA and proxies `/api`, so there's no
  browser CORS. `CORS_ALLOWED_ORIGINS` is still set to `https://$DOMAIN` because
  production config requires it.
- **Named `appdata` volume** for PDFs survives restarts but not host loss / can't
  be shared across hosts → migrate to object storage (R2) in **issue #6** before
  scaling past one box.

## Known follow-ups after "live"

| Concern | Issue |
|---|---|
| PDFs → object storage (R2) | #6 |
| Scraper fleet split + residential proxies | #23 (Track D) |
| Off-box backups + error tracking | #9 |
| Self-serve signup / connect-client-mailbox email | #2 / #1 |
