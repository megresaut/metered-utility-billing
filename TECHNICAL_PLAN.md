# Utility Billing Platform — Technical Plan (MVP)

## Context

This is a standalone productization of the utility-bill scraping/extraction engine that
already exists inside a separate internal system called `ra-avm`, located on this machine at
`/Users/megkrish/Desktop/ra-avm/dev`. That system is a property-management back office for one
company (AVMRE) and must NOT be modified. Treat `/Users/megkrish/Desktop/ra-avm/dev` as
**read-only source material** — copy code out of it, never write to it, never add it as a
dependency (no shared packages, no imports back into it, no git submodule). This new project is
fully isolated: once code is copied out, it diverges independently forever.

Goal: a working, demoable, multi-tenant MVP that can capture utility bills (via automated
scraping or manual upload), extract structured data with AI, centralize them in a dashboard, and
export them for accounting import — good enough to run a live demo and onboard a small number of
pilot clients. It does not need to be fully hardened (see Non-Goals).

## Pull vs. build — at a glance

| Piece | Action |
|---|---|
| `scrapers/providers/*` (16 providers) | **Copy verbatim**, unmodified, into `scrapers/providers/` |
| `scrapers/common/*` | **Copy verbatim** into `scrapers/common/` |
| `scrapers/parsers/invoice_parser.py` (+ service) | **Copy verbatim** into `scrapers/parsers/` |
| `api/modules/utilities/worker.go` | **Copy the file**, then **edit in place** (strip + add org_id) — see below |
| `scheduler.go` / `batch_redis.go` / `scrape_schedule.go` | **Do not copy.** Read for the pattern only, then **write new**, simpler code |
| `providers`/`utility_accounts`/`scrape_jobs`/`bills` schema | **Copy table structure**, then **strip listed columns** — see Schema section |
| `organizations`, `org_users`, `properties`, `export` modules | **Build new** — no ra-avm equivalent exists |
| Credential encryption | **Build new** — do not port ra-avm's scheme (see below) |
| Frontend (`web/`) | **Build new** from a fresh scaffold — do not copy files from `builder_frontend` (see Target architecture) |

## Source material to copy (read, don't modify, from `ra-avm/dev/avm-backend/`)

- `scrapers/providers/*` — 16 provider scrapers, already credential/config-driven, not tied to
  any specific client: `aquarion`, `cng`, `eversource`, `fdwd`, `fios`, `frontier`, `optimum`,
  `rwa`, `santaguida`, `scg`, `snew`, `starlink`, `ttd`, `uinet`, `winwaste`, `wpca`. Copy the
  directories verbatim; no edits needed to make them run standalone.
- `scrapers/common/` — `browser.py`, `stealth.py`, `pdf.py`, `http_client.py`, `xvfb.py`,
  `config.py`, `types.py`. Copy verbatim.
- `scrapers/parsers/invoice_parser.py` (+ `invoice_parser_service.py`) — Claude-based structured
  extraction from bill PDFs (vendor, amount, service dates, due date). Copy verbatim.
- `api/modules/utilities/worker.go` — job dispatch logic. **Important**: this file mixes a
  generic, capabilities-driven dispatch path (reads `providers.capabilities.launch.argv` from the
  DB and renders `{secret.password}`-style placeholders into CLI args) with hardcoded per-provider
  branches for at least Aquarion, Optimum, Eversource, CNG/FDWD, and one provider that runs inside
  a Docker container. **Copy this file into the new repo's `api/modules/utilities/` directory as
  the starting point, then edit it in place**: strip anything AVMRE-specific (Slack alert webhooks,
  Outlook integration, Buildium/QBO references) and add `org_id` to every query/struct it touches.
  Do not rewrite it from scratch, and do **not** spend MVP time consolidating the hybrid dispatch
  into one clean path — edit the copy as-is and only fix a provider's dispatch if it's actually
  broken for a real pilot account.
- `api/modules/utilities/scrape_schedule.go`, `scheduler/scheduler.go`, `batch_redis.go` — **do
  not copy these files.** Read them to understand the scheduling/batching pattern, then write new,
  simpler code: a plain DB-polled queue is sufficient for MVP unless you judge Redis batching is
  trivial to add, in which case log that as a decision and add it — but the new code should be
  original, not a copy, since these files are the most AVMRE-integration-entangled of the bunch.
- Schema reference (from `ra-avm/dev/avm-backend/ra_clean_schema.sql`): `providers`,
  `utility_accounts`, `scrape_jobs`, `bills`. Copy the **structure**, not the data, and strip
  AVMRE/accounting-specific columns (e.g. `buildium_vendor_id`, `buildium_gl_code`,
  `qbo_vendor_id`, `coa_id`, `qbo_item_id`, `billing_mode`, `skip_outlook_draft` on `providers` —
  none of that is needed since Auto-Post is out of scope for this MVP). Do **not** carry over
  `bill_lines` or `bill_attachments` — for MVP, one bill = one row = one PDF; fold the PDF
  reference directly into `bills.pdf_object_key` instead of a separate attachments table.

## Known issue to fix, not carry forward

`resolveSecrets()` in `ra-avm`'s `worker.go` "encrypts" credentials by base64-encoding them
(`credential_ref` prefixed `enc:`) and falls back to reading plaintext values from server
environment variables (`CRED_<REF>_PASSWORD`). Neither is real encryption. This new system will
hold other companies' utility-portal logins, so credential storage must actually be encrypted from
day one — see Credential Storage below. Do not port the base64/env-var scheme.

## Target architecture

```
utility-billing-platform/
├── api/                        # Go REST API, mirrors ra-avm's module pattern
│   ├── cmd/server/main.go
│   ├── middleware/             # JWT auth; every request resolves org_id into context
│   └── modules/
│       ├── orgs/               # organizations, org_users, minimal auth (login only, no self-serve signup)
│       ├── properties/         # thin model: org_id, name, address
│       ├── utilities/          # ported scraper-job dispatch + scheduling, org_id threaded through
│       └── export/             # CSV bulk-export generator
├── scrapers/                    # copied verbatim: providers/, common/, parsers/
├── migrations/                   # new SQL migrations, sequential numbering starting at 001
├── web/                          # frontend — fresh scaffold, not copied from anywhere
└── DECISIONS.md                  # running decision log (see agent instructions)
```

Stack: Go + `pgxpool` (jackc/pgx v5) + raw parameterized SQL, no ORM — same as ra-avm.
Python + Playwright for scrapers, using the copied `common/` utilities — same as ra-avm.
React + TypeScript + Vite + Tailwind for the frontend, matching ra-avm's general stack choice —
but **do not copy any files from `builder_frontend`**, including `client/components/ui/`. That
app's component primitives carry path aliases, theme tokens, and app-specific wiring that would
be brittle to partially extract. Instead run a fresh `npm create vite` scaffold and install
Tailwind (+ shadcn/ui if useful for speed) from scratch. This is a deliberate build-new choice,
not an oversight — do not spend time trying to extract ra-avm's UI primitives.

Run on different ports than ra-avm so both can run simultaneously on this machine: API on
`:8090`, frontend dev server on `:5174`. Use a new local Postgres database, e.g.
`utility_billing_platform_local` — create it fresh, do not touch `ra_avm_local_final`.

### Multi-tenancy

`organizations` table at the top; `org_id` on every business table (`properties`,
`utility_accounts`, `scrape_jobs`, `bills`). Enforce isolation at the application layer: JWT
carries an `org_id` claim, middleware puts it in request context, every repository query filters
`WHERE org_id = $1`. Postgres row-level security is explicitly out of scope for MVP — application-
level filtering is an accepted trade-off given the small number of trusted pilot clients.

### Credential storage

AES-256-GCM, single application-level master key read from an environment variable
(`CRED_MASTER_KEY`), random nonce per row, ciphertext + nonce stored in `utility_accounts`.
Decrypt only at the moment a scrape job is dispatched; never log secret values. This is
deliberately simpler than per-tenant envelope encryption / KMS (that's a hardening item for after
pilot validation) but it is real encryption, not obfuscation.

### Job execution

Same subprocess-per-job model as ra-avm: the Go API shells out to the relevant provider's
`scraper.py` via `exec.CommandContext`, passing credentials as CLI args (decrypted just-in-time)
or via the capabilities/argv templating path. Thread `org_id` through job records for correct
attribution and dashboard filtering.

### Export

New module producing a CSV (vendor, amount, due date, service dates, property, account number) —
human-reviewable and bulk-import-ready, but it does not need to match any specific accounting
platform's exact import spec for MVP.

## Schema (starting DDL — adjust as needed, but keep this shape)

```sql
organizations(id, name, created_at)
org_users(id, org_id, email, password_hash, role, created_at)
properties(id, org_id, name, address, created_at)
providers(id, code, display_name, category, capabilities jsonb, created_at)
  -- seed with the same 16 providers/capabilities as ra-avm's `providers` table
utility_accounts(id, org_id, property_id, provider_id, account_number, service_address,
  username, credential_ciphertext, credential_nonce, active, metadata jsonb,
  next_scheduled_scrape_at, last_scheduled_run_at, consecutive_scrape_failures, created_at)
scrape_jobs(id, org_id, utility_account_id, status, attempt, max_attempts, started_at,
  finished_at, error_code, error_message, params jsonb, requested_at)
bills(id, org_id, utility_account_id, provider_id, amount_cents, due_date, service_start,
  service_end, status, pdf_object_key, created_at)
```

## API surface (MVP)

- Auth: `POST /login` (JWT). No self-serve signup — org/org_user creation is admin/CLI-driven.
- `orgs`, `properties`, `utility_accounts`: CRUD, admin-facing (doesn't need to be polished).
- `bills`: list (filterable by org/property/status), detail, `POST /bills/upload` (multipart PDF
  → `invoice_parser` → new `bills` row).
- `scrape-jobs`: trigger a manual scrape, list job status.
- `GET /export/csv?org_id=...`

## Non-goals for this pass (do not spend time here)

- QuickBooks/Buildium live posting ("Auto-Post" tier) — CSV export covers this for now.
- Self-serve signup flow.
- Consolidating the 16 providers onto a single clean dispatch path.
- Stripe billing/metering.
- Postgres row-level security, per-tenant key rotation, audit trail.
- New provider scrapers beyond the existing 16.
- Notification emails/digests.
