# Metered — Utility Billing Platform (MVP)

Multi-tenant sidecar product for property management firms: captures utility
bills (automated portal scraping for 16 providers, or manual PDF upload),
extracts structured data with AI, centralizes everything in one dashboard, and
exports an accounting-import-ready CSV.

See `TECHNICAL_PLAN.md` / `FEATURE_PLAN.md` for scope and `DECISIONS.md` for
the build decision log.

## Monorepo layout

```
api/         Go REST API (cmd/server, cmd/admin, modules/…)
scrapers/    Python + Playwright provider scrapers + Claude invoice parser
migrations/  Postgres schema + provider seed
web/         React + Vite + Tailwind frontend
scripts/     demo-data seeding
```

## Live demo build (Vercel)

The `web/` app builds in a **demo mode** (`VITE_DEMO=1`) that serves a bundled
data snapshot from `web/public/demo/*.json` instead of calling the Go API — so
a static host (Vercel) renders the full product (login, dashboard, bills,
reports, PDF preview, CSV export) with no backend, Postgres, or scrapers
running. Mutations (mark paid, etc.) apply in-memory and reset on reload. The
normal build (`VITE_DEMO` unset) talks to the real API and is unchanged.

Deploy: Vercel project rooted at `web/`, build env `VITE_DEMO=1`.

## Stack

- **API** — Go (stdlib router + pgx/v5, raw SQL), port **8090**
- **Scrapers** — Python + Playwright (16 providers, ported from ra-avm) +
  Claude-based invoice parser
- **Web** — React + TypeScript + Vite + Tailwind, dev server port **5174**
- **DB** — Postgres, database `utility_billing_platform_local`

## Fresh-checkout setup

Prereqs: Go ≥ 1.24, Node ≥ 20, Python 3.11 or 3.12, Postgres running locally.

```bash
# 1. Database
createdb utility_billing_platform_local

# 2. Environment — copy and fill in:
cat > .env <<EOF
DATABASE_URL=postgres://localhost:5432/utility_billing_platform_local?sslmode=disable
PORT=8090
JWT_SECRET=$(openssl rand -hex 24)
CRED_MASTER_KEY=$(openssl rand -hex 32)   # AES-256 key for portal credentials
ANTHROPIC_API_KEY=sk-ant-...              # for AI bill extraction
EOF

# 3. Scraper environment (venv + Playwright Chromium)
./scrapers/setup.sh

# 4. API (runs migrations automatically on boot)
cd api && go run ./cmd/server

# 5. Create the first org + login (no self-serve signup)
cd api
go run ./cmd/admin create-org  --name "Acme Property Management"
go run ./cmd/admin create-user --org-id 1 --email you@acme.com --password changeme

# 6. Frontend
cd web && npm install && npm run dev    # http://localhost:5174
```

Or, with `.env` in place: `make api`, `make web`, `make scrapers-setup`.

## The demo flow

1. Sign in → **Properties** → add a property or two.
2. **Utility Accounts** → add accounts: leave credentials blank for
   manual-only, or enter real portal credentials to enable auto-scrape
   (encrypted with AES-256-GCM; decrypted only at dispatch time).
3. **Bills → Upload bill PDF**: drop any bill PDF; AI extracts vendor, amount,
   statement/due dates and service period, and the bill appears with its PDF.
4. **Utility Accounts → Scrape now** (or wait for the hourly scheduler):
   pulls the latest bill from the provider portal; progress on **Scrape Jobs**.
5. **Bills → Export CSV**: accounting-import-ready file (amounts in dollars,
   ISO dates, property + account number per row).

## Operational notes

- **Scheduler**: hourly tick (`SCHEDULER_INTERVAL_SEC`); accounts with
  credentials are scraped when `next_scheduled_scrape_at` elapses. Success
  advances the slot to `service_end + 1 month + 1 day`; scheduler-driven
  failures retry weekly up to 3× then wait for the next natural cycle.
- **2FA providers (Aquarion, WinWaste)**: when the portal emails a code, write
  it to `data/handoff/<provider>/2fa_code.txt` while the scraper waits — the
  worker logs the exact path per run.
- **Provider stability**: prefer stable providers for demos. Optimum has a
  documented history of breakage (residential-proxy dependency) — don't lead
  a demo with it.
- Bill PDFs are stored under `data/store/` (`STORE_ROOT`); bills reference
  them by object key.
- Multi-tenancy is enforced at the application layer: every JWT carries
  `org_id`, every query filters on it. RLS/per-tenant keys are post-pilot
  hardening items.

## Layout

```
api/            Go API (cmd/server, cmd/admin, modules/{orgs,properties,utilities,export})
scrapers/       Python scrapers: providers/ (16), common/, parsers/ (copied from ra-avm)
migrations/     Sequential SQL migrations (applied automatically at API boot)
web/            React frontend
data/           Local PDF store + 2FA handoff dirs (gitignored)
```
