# Decision Log

Running log of decisions made during the autonomous MVP build where the plans
left room for judgment. Newest last.

1. **Product/brand name: "BillFlow".** FEATURE_PLAN.md allowed choosing a
   marketing-friendly name instead of the "Utility Billing Platform"
   placeholder. Used in page titles, the login screen, and the dashboard
   header. Low-stakes and reversible (one string in `web/`).

2. **Go module name `ubp`, stdlib `net/http` router.** Go 1.22+ pattern
   routing (`GET /api/bills/{id}`) covers the whole API surface — no router
   dependency. Total Go deps: pgx/v5, golang-jwt/v5, x/crypto (bcrypt).

3. **No Redis.** The plan allowed Redis batching "if trivial"; it isn't needed
   for a single-instance MVP. The scheduler is a plain DB-polled loop
   (SELECT … FOR UPDATE SKIP LOCKED), the worker claims jobs the same way, and
   the Aquarion 2-minute spacing rule uses an in-process timestamp instead of
   a Redis key. Job progress is exposed via polling `GET /api/scrape-jobs`
   (the frontend polls every 4s) instead of ra-avm's WebSocket hub.

4. **Credential plaintext is a JSON secrets object**, not a bare password:
   `{"password": "...", "sec_answer": "..."}` encrypted with AES-256-GCM
   (single master key from `CRED_MASTER_KEY`, random 12-byte nonce per row).
   This lets providers that need more than a password (Fios security answer)
   use the same scheme. Replaces ra-avm's base64/env-var scheme, and removes
   worker.go's hardcoded `"Jeff"` Fios fallback — a missing sec_answer is now
   a clear dispatch error.

5. **bills schema kept the planned shape plus a few working columns:**
   `statement_date`, `statement_id`, `sha256_pdf` (needed by the ported worker
   and for scrape dedup), `vendor_name` (uploads have no provider row),
   `property_id` directly on bills (uploads may not have a utility account),
   `parse_confidence`, and `source` (`scrape` | `upload`). `status` stores only
   `outstanding` | `paid`; **overdue is derived at read time**
   (`outstanding AND due_date < today`) so it never goes stale.

6. **Scrape dedup**: unique index on
   `(utility_account_id, statement_date, amount_cents) WHERE source='scrape'`
   replaces ra-avm's dedup constraint. Duplicate-key failures are funneled into
   the same +7d/backoff retry policy as other failures (semantics preserved
   from ra-avm's scrape_schedule.go), but silently (no alerting — Slack was
   stripped; failures go to the server log).

7. **Scheduling formula simplified**: `providers.billing_mode` was stripped
   per plan, so the next-scrape slot is always `max(service_end) + 1 month +
   1 day @ 06:00 ET` (ra-avm's "arrears" formula, which applied to nearly all
   providers anyway). New-account bootstrap: accounts created *with*
   credentials get `next_scheduled_scrape_at = now()` so the next scheduler
   tick (hourly by default) picks them up.

8. **2FA handoff is manual for MVP.** ra-avm polled an AVMRE Outlook mailbox
   for Aquarion/WinWaste 2FA codes. That integration was stripped; the worker
   now logs the handoff path (`data/handoff/<provider>/2fa_code.txt`) and an
   operator can drop the emailed code there while the scraper waits (the
   Python side already polls for the file). Automating this is a post-pilot
   item.

9. **worker.go dispatch edits beyond the planned strips** (fixes for things
   actually broken standalone, per the plan's "only fix if broken" rule):
   - `runGeneric` (cwd `<scrapers>/<provider>`, no PYTHONPATH) could not work
     in this repo layout; the default branch now uses the module path
     (`python -m providers.<code>.scraper`) like every other branch. The
     stray "kisk" provider branch (not one of the 16) was dropped.
   - All hardcoded `/Users/megkrish/Desktop/ra-avm/scrapers` fallbacks removed;
     paths come from config only.
   - Optimum's docker-container self-heal is kept but only runs when
     `SCRAPER_CONTAINER` is explicitly set (ra-avm defaulted to its own
     container name).
   - Debug logging that wrote into the ra-avm tree (`.cursor/debug.log`) was
     removed outright.
   - Command logging no longer prints full argv (it contained `--password`);
     secrets are never logged.

10. **invoice_parser.py got one additive prompt edit**: extract
    `service_start` / `service_end`. The copied prompt didn't extract service
    dates, but FEATURE_PLAN.md explicitly lists them as extracted fields and
    the CSV export needs them. Copies diverge independently per plan, and the
    parser's model/output contract is otherwise unchanged.

11. **Scraper env pins**: PyPI no longer has patchright 1.47.x, so
    `requirements.txt`'s original `playwright==1.47.0 + patchright>=1.47.0`
    pair is unresolvable today. `scrapers/setup.sh` reproduces the known-good
    combination from the working ra-avm venv (playwright 1.47.0 installed
    first, then patchright 1.58.0 on top) and prefers Python 3.11/3.12
    (playwright 1.47's pinned greenlet 3.0.3 has no 3.13 wheels).

12. **ANTHROPIC_API_KEY copied from ra-avm's local `.env`** into this
    project's `.env` (same machine, same owner) so the invoice parser works
    out of the box. `.env` is gitignored; a fresh deployment needs its own key.

13. **PDF/CSV links authenticate via `?token=` fallback** in the auth
    middleware (iframes and download links can't set an Authorization header).
    Acceptable for MVP; cookie-based auth is the hardening path.

14. **No live scrapes with borrowed production credentials.** The scrape
    dispatch chain was verified end-to-end against a real provider portal
    (Regional Water) using throwaway credentials: AES-GCM decrypt → Python
    subprocess → Playwright browser → live portal login attempt → graceful
    failure recorded on the job with the retry policy applied. Actually
    *confirming* providers bill-to-dashboard requires real portal logins;
    ra-avm's production credentials were deliberately not reused for
    autonomous live scraping (risk of lockouts/2FA side effects on real
    accounts). Enter real credentials on the Utility Accounts page when
    setting up a pilot/demo and use "Scrape now".

15. **SaaS build-out (2026-07-17, on request).** To sell this as a standalone
    SaaS product, the frontend gained Reports (property×month pivot, category
    breakdown, top vendors), an Integrations catalog page (16 providers with
    connected state), Settings (org/plan/team/security/data-export), a
    "Capture Log" rename for scrape jobs, an upgraded dashboard (category
    donut, upcoming payments, auto-capture rate), and a plan badge. One API
    addition: `GET /api/org-users` for the team list. The demo portfolio was
    expanded to 4 properties / 14 accounts / 90 bills spanning Jan–Jul 2026.
    Historical bills were backfilled via SQL **with real generated PDFs in
    the store** (every bill detail page renders its PDF) and seasonal amounts
    (gas high in winter, electric in summer); the original 6 bills remain
    real-pipeline AI extractions and were linked to their accounts. Seeded
    auto-scrape accounts use dummy encrypted credentials with
    next_scheduled_scrape_at pushed weeks out so the hourly scheduler never
    fires seed credentials at real portals. Scrape-job history rows were
    seeded in terminal states only (worker only claims 'queued', so it never
    touches them). Seed script: scripts/seed_demo_portfolio.py (idempotent for properties/accounts; bill inserts append).

16. **Design system v2 (2026-07-17, user feedback: v1 "a bit tacky").**
    Replaced the saturated indigo/Tailwind-default look with a restrained,
    neutral-first system: Inter (via @fontsource-variable), warm stone
    neutrals, near-black primary buttons, hairline-border cards, inline SVG
    stroke icons replacing unicode glyphs, and status rendered as a small
    colored dot + neutral label instead of colored badge pills (source shown
    as a quiet AUTO/MANUAL outline chip). Data-viz colors now follow the
    dataviz skill's method: property series use a CVD-validated categorical
    palette (slot order #2a78d6/#008300/#e87ba4/#eda100 — validated with the
    palette script on a white surface, order is the safety mechanism, don't
    re-order), single-measure category bars use one hue (#2a78d6) since
    labels carry identity, the donut was removed, and chart chrome uses
    muted ink with hairline grids. Sidebar simplified (black wordmark tile,
    "Overview" naming, plan shown as a small PILOT chip in the footer).

17. **Demo data is real-pipeline data**: the seeded bills were generated as
    PDF files and pushed through the actual upload → Claude extraction path
    (not inserted via SQL), so the dashboard demonstrates real extraction
    output end-to-end.
