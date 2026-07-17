# Utility Billing Platform — Feature Plan (MVP)

## Product framing

A sidecar automation product sold to any property management firm (not just concierge/luxury),
regardless of what PM or accounting software they already run. It does not replace their existing
system of record — it captures utility bills, extracts the data with AI, centralizes them in one
dashboard, and exports them for accounting import. This MVP exists to support a marketing site and
live pilot demos, not a public self-serve launch.

Placeholder name: "Utility Billing Platform." If a better product/brand name is useful for the
marketing surface (page titles, dashboard header), choose one and record it as a decision in
`DECISIONS.md` rather than asking — this is a low-stakes, reversible choice.

## Feature tiers in scope for MVP

1. **Capture** — manual PDF upload. Zero integration required. A user drops in a bill PDF, AI
   extracts vendor/amount/service dates/due date, it appears in the dashboard. This should be the
   most reliable, most "always works" path — it's the fastest thing to demo.
2. **Capture + Auto-Scrape** — automated login and pull for any of the 16 covered providers, once
   an admin has entered real credentials for a utility account. Same centralized dashboard
   experience as Capture; the difference is bills arrive without anyone uploading anything.
3. **Export** — a CSV bulk-export button producing an accounting-import-ready file. This is the
   whole "posting" story for MVP; there is no live push into QuickBooks/Buildium/etc.

Auto-Post (live push into a specific accounting platform) is explicitly **not** part of this MVP.
If asked to demo it, the CSV export is the answer — frame it in any UI copy as "one-click import,"
not manual data entry.

## Core end-to-end flow the MVP must support without intervention

1. An admin (internal, not a client-facing signup flow) creates an organization and a couple of
   properties under it.
2. The admin adds utility accounts under those properties — some marked manual-only, some with
   real credentials for providers confirmed to be working.
3. Bills flow in, either via a scheduled/triggered scrape or a manual PDF upload, get AI-extracted,
   and appear in the dashboard with a status (outstanding / overdue / paid) and provider.
4. A user (playing the role of the client) opens the dashboard: sees the bill list across
   properties, per-property spend history, and can open a bill to see the extracted fields next to
   the source PDF.
5. The user exports a CSV of the current bills.

## Success criteria — "this is marketing-ready"

- Steps 1–5 above work end-to-end with real data (at least one real working scraper provider, plus
  the manual-upload path), without manual debugging or intervention, on a fresh checkout of this
  project.
- The dashboard is visually presentable enough to screenshot for a marketing site (doesn't need to
  be pixel-perfect, needs to not look like an internal admin tool).
- The CSV export produces a file that's actually usable — correct amounts (in dollars for display,
  cents internally per the cents-storage convention), correct dates, no missing required fields for
  the bills present.
- At least a few of the 16 providers are confirmed working end-to-end during the build (pick the
  ones most likely to be stable — avoid leading a demo with a provider known to have issues, e.g.
  Optimum has a documented history of scraper breakage tied to a residential proxy dependency,
  so don't rely on it as your primary demo provider unless it's verified working during the build).

## Explicit non-goals (mirrors the technical plan)

- No live accounting posting (QBO/Buildium).
- No self-serve client signup.
- No SMS/vendor-outreach/maintenance features — that's a separate future product ("Maintenance
  Hub"), entirely out of scope here.
- No billing/subscription enforcement — pilot clients are invoiced manually outside this system.
- No new provider integrations beyond the 16 already built.

## Nice-to-have, only if time allows after the above is solid

- Email digest of new/outstanding bills.
- Simple per-property spend chart (bar or line, monthly totals) — this is a good marketing
  screenshot if it's easy to add; skip if it risks the core flow.

Do not let nice-to-haves delay the core end-to-end flow in "Success criteria" above.
