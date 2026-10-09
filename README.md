# Metered

**Utility bills for property-management firms, captured automatically.**

**Live demo:** https://metered-demo.vercel.app/

Metered collects every utility bill across a property portfolio, from electric and gas to water, sewer, trash, and internet. It logs into provider portals on a schedule, pulls each new bill, and uses AI to read it. Everything lands in one dashboard, and accounting gets a clean import file. It works alongside whatever property-management and accounting software a firm already uses.

---

## The problem

A firm with a few dozen properties may have over a hundred utility accounts across more than a dozen providers. Each provider has its own portal, password, and billing cycle. Each month someone has to log into every portal, download every PDF, and type the amount, dates, and account number into a spreadsheet or accounting system. They also have to notice the one bill that didn't arrive before it goes overdue. Metered does that work.

## What it does

### Capture
- **Automatic portal capture.** Metered logs into **16 utility provider portals** for you: Eversource, United Illuminating, Southern CT Gas, Connecticut Natural Gas, Aquarion, Regional Water, Verizon Fios, Optimum, Frontier, Starlink, WinWaste, and more, covering electric, gas, water, sewer, waste, and internet. It downloads new bills on each account's billing cycle.
  - If a scrape fails, it retries on its own.
  - For portals that require a two-factor code, the scraper pauses and waits for someone to supply the code.
- **Manual upload.** Drag in any bill PDF, from any provider, for accounts that aren't automated.
- **Capture Log.** Every scrape run is logged with its status, so you can see which accounts captured cleanly and which need attention.

### AI extraction
Every bill, scraped or uploaded, is read by AI. It extracts the **vendor, amount due, statement date, due date, and service period**. The bill detail page shows those fields **next to the original PDF** so anyone can check them at a glance.

### One dashboard for the whole portfolio
- **Dashboard:** total spend, upcoming payments, overdue bills, spend by category, and the share of bills captured automatically.
- **Bills:** every bill across all properties, filterable, marked outstanding, overdue, or paid.
- **Properties:** a page per property with its utility accounts, bill history, and spend over time.
- **Reports:** monthly spend by property, spend by utility category, top vendors, and a portfolio total.

### Export to accounting
**One-click CSV export** produces an accounting-import-ready file: amounts in dollars, ISO dates, and property and account number on every row. There's no re-keying.

### Built for multiple firms
- Each customer firm is a separate **organization**. Data is isolated in the application and again by Postgres **row-level security**.
- Portal passwords are **encrypted at rest (AES-256-GCM)** and only decrypted at the moment a scrape runs.
- **Integrations** shows each supported provider and which accounts are connected. **Settings** covers the organization, team, security, and data export.
- Customer firms are set up by an admin. There's no public self-serve signup.

## Out of scope, on purpose
- No direct posting into QuickBooks, Buildium, or other accounting systems. The CSV export is the hand-off.
- No bill payment.
- No maintenance or vendor features. Those are a separate product, [Maintenance Hub](https://github.com/megresaut/maintenance-hub-platform).

## Tech

- **API and worker:** one Go process (standard library router, pgx/v5, raw SQL) that serves the API, runs the scrape queue, and runs the hourly scheduler.
- **Scrapers:** Python and Playwright, one module per provider. Bills are parsed by extracting text with pdfplumber and passing it to an LLM via OpenRouter.
- **Web:** React, TypeScript, Vite, Tailwind.
- **Database:** PostgreSQL with row-level security. Bill PDFs are stored on disk.
- **Hosting:** Docker container with Postgres and Caddy (`deploy/ovh/`), with CI/CD through GitHub Actions. A no-backend demo build (`VITE_DEMO=1`) runs on Vercel.

```
api/         Go API, scrape worker, scheduler, and admin CLI
scrapers/    provider scrapers and AI bill parser
migrations/  Postgres schema and provider seed data
web/         React frontend
deploy/      production deploy (OVH)
```

---

## Setup

Requires Go 1.24+, Node 20+, Python 3.11/3.12, and PostgreSQL.

```bash
createdb utility_billing_platform_local
# .env at repo root: DATABASE_URL, JWT_SECRET, CRED_MASTER_KEY (openssl rand -hex 32), OPENROUTER_API_KEY

./scrapers/setup.sh                                   # venv + Playwright Chromium
cd api && go run ./cmd/server                         # :8090, runs migrations
go run ./cmd/admin create-org  --name "Acme PM"
go run ./cmd/admin create-user --org-id 1 --email you@acme.com --password changeme
cd ../web && npm install && npm run dev               # http://localhost:5174
```

More detail: `FEATURE_PLAN.md` (product scope), `TECHNICAL_PLAN.md` (architecture), `DECISIONS.md` (build log), `DEPLOY.md` and `deploy/ovh/README.md` (production).
