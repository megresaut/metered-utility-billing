# Metered — Utility Billing Platform

## What this is (read this first)

**Metered** is a multi-tenant **sidecar product for property-management firms**. It
captures utility bills, extracts the data with AI, centralizes everything in one
dashboard, and exports an accounting-import-ready CSV. It is a **sidecar** — it does
*not* replace a firm's existing PM/accounting system of record.

The three product tiers:

1. **Capture (manual)** — drop in a bill PDF; an LLM (via OpenRouter) extracts vendor / amount /
   statement + due dates / service period; it appears in the dashboard. This is the
   most reliable, "always works" demo path.
2. **Capture + Auto-Scrape** — automated portal login + bill pull for any of **16
   covered providers**, once an admin enters real credentials for a utility account.
3. **Export** — one CSV bulk-export button producing an accounting-import-ready file.
   This *is* the whole "posting" story for the MVP.

This MVP exists to support a **marketing site + live pilot demos**, not a public
self-serve launch.

## ⚠️ Scope boundaries — do not build these here

- **No maintenance features.** SMS / vendor-outreach / maintenance is a *separate
  future product* ("Maintenance Hub") and is **entirely out of scope**. If someone
  refers to this as a "maintenance platform," that is a misnomer — it is a
  **utility billing** platform.
- **No live accounting posting** (QuickBooks / Buildium). CSV export covers it. Frame
  export in UI copy as "one-click import," never "manual data entry."
- **No self-serve signup.** Orgs and users are created by admin/CLI only.
- **No Stripe / billing / subscription enforcement.** Pilots are invoiced manually.
- **No new provider scrapers** beyond the existing 16.
- **Per-tenant key rotation / audit trail** — post-pilot hardening. (Postgres
  **RLS is now in place** as of `003_rls.sql` — see the tenant-isolation note below.)

## Origin & the read-only source

This is a standalone productization of the scraper/extraction engine that lives inside
an internal system called **`ra-avm`** at `/Users/megkrish/Desktop/ra-avm/dev`. That
tree is **READ-ONLY source material** — copy code out of it, never write to it, never
import it, never add it as a dependency. Once code was copied, the two diverge forever.

## Stack & layout

- **API** — Go (stdlib `net/http` router, Go 1.22+ pattern routing; pgx/v5 + raw SQL,
  no ORM). Module name `ubp`. Port **8090**. Runs migrations automatically on boot.
- **Scrapers** — Python + Playwright, 16 providers (ported from ra-avm) + an
  OpenRouter-based invoice parser (pdfplumber text → cheap LLM, model set by
  `OPENROUTER_MODEL`). venv is fragile — see gotchas below.
- **Web** — React + TypeScript + Vite + Tailwind. Dev port **5174**.
- **DB** — Postgres, database `utility_billing_platform_local`.

```
api/         Go REST API — cmd/{server,admin}, modules/{orgs,properties,utilities,export}
scrapers/    Python scrapers: providers/ (16), common/, parsers/ (invoice_parser.py)
migrations/  001_init.sql, 002_seed_providers.sql (applied automatically at API boot)
web/         React frontend (src/pages/*, lib/{api,demo,insights}.ts)
data/        Local PDF store (STORE_ROOT) + 2FA handoff dirs (gitignored)
scripts/     seed_demo_portfolio.py
```

## Running it

- `make api` / `make web` / `make scrapers-setup` (needs `.env` in place).
- Demo login: **demo@harborview.example / demo1234** (org 1, Harborview Property Mgmt).
- First org/user on a fresh checkout: `go run ./cmd/admin create-org` /
  `create-user` (no self-serve signup).

## Key facts not obvious from the code

- **`.env` is gitignored** and holds `CRED_MASTER_KEY` (AES-256 key for portal creds)
  + `ANTHROPIC_API_KEY` (copied from ra-avm's `.env` on this machine). A fresh
  deployment needs its own keys.
- **Scraper venv pin order matters**: Python 3.11/3.12, install `playwright==1.47.0`
  **first**, then `patchright 1.58.0` on top (`scrapers/setup.sh`). The old
  requirements pin is unresolvable on PyPI today.
- **Credentials** are stored as an AES-256-GCM–encrypted JSON secrets object
  (`{"password":..., "sec_answer":...}`), decrypted only at scrape-dispatch time,
  never logged.
- **`status` is only `outstanding | paid`** in the DB — **overdue is derived at read
  time** (`outstanding AND due_date < today`) so it never goes stale.
- **Scheduler** = plain hourly DB-polled loop (`FOR UPDATE SKIP LOCKED`), no Redis.
  Job progress is exposed by polling `GET /api/scrape-jobs` (frontend polls ~4s).
- **2FA (Aquarion, WinWaste)** is manual for MVP: drop the emailed code into
  `data/handoff/<provider>/2fa_code.txt`; the worker logs the exact path.
- **Provider stability**: prefer stable providers for demos. **Optimum** has a
  documented breakage history (residential-proxy dependency) — don't lead with it.
- **No live scrapes have been run with real production credentials.** The dispatch
  chain was verified end-to-end against a real portal with *throwaway* creds only.
  Confirming a provider bill→dashboard requires real logins at pilot setup.

## Demo mode (Vercel build)

`web/` builds in a **demo mode** (`VITE_DEMO=1`) that serves a bundled snapshot from
`web/public/demo/*.json` instead of calling the Go API — a static host renders the full
product (login, dashboard, bills, reports, PDF preview, CSV export) with **no backend,
Postgres, or scrapers**. Mutations apply in-memory and reset on reload. The normal build
(`VITE_DEMO` unset) talks to the real API and is unchanged. Deploy: Vercel project
rooted at `web/`, build env `VITE_DEMO=1`.

## The decision log

**`DECISIONS.md`** at the repo root is the running build-decision log — read it before
changing scheduling, credential handling, worker dispatch, schema, or the design
system. When a plan leaves room for judgment, record the choice there rather than
asking (per the original build instructions). Product scope lives in `FEATURE_PLAN.md`;
architecture in `TECHNICAL_PLAN.md`.
