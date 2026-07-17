-- 001_init.sql — core multi-tenant schema.
-- Structure derived from ra-avm's providers/utility_accounts/scrape_jobs/bills,
-- with org_id threaded through every business table and all AVMRE/accounting
-- columns (buildium_*, qbo_*, coa_id, billing_mode, skip_outlook_draft,
-- bill_lines, bill_attachments) stripped per TECHNICAL_PLAN.md.

CREATE TABLE organizations (
    id bigserial PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE org_users (
    id bigserial PRIMARY KEY,
    org_id bigint NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role text NOT NULL DEFAULT 'admin' CHECK (role IN ('admin', 'member')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE properties (
    id bigserial PRIMARY KEY,
    org_id bigint NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name text NOT NULL,
    address text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX properties_org_idx ON properties(org_id);

-- Providers are global (not per-org): the 16 scraper integrations.
CREATE TABLE providers (
    id bigserial PRIMARY KEY,
    code text NOT NULL UNIQUE,
    display_name text NOT NULL,
    category text NOT NULL,
    capabilities jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE utility_accounts (
    id bigserial PRIMARY KEY,
    org_id bigint NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    property_id bigint NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    provider_id bigint NOT NULL REFERENCES providers(id),
    account_number text NOT NULL,
    service_address text,
    username text,
    -- AES-256-GCM: ciphertext of the portal password, random nonce per row.
    -- NULL for manual-only accounts (no scraping).
    credential_ciphertext bytea,
    credential_nonce bytea,
    active boolean NOT NULL DEFAULT true,
    metadata jsonb NOT NULL DEFAULT '{}',
    next_scheduled_scrape_at timestamptz,
    last_scheduled_run_at timestamptz,
    consecutive_scrape_failures integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX utility_accounts_org_idx ON utility_accounts(org_id);
CREATE INDEX utility_accounts_property_idx ON utility_accounts(property_id);

CREATE TABLE scrape_jobs (
    id bigserial PRIMARY KEY,
    org_id bigint NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    utility_account_id bigint NOT NULL REFERENCES utility_accounts(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempt integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3,
    requested_by text,
    requested_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    error_code text,
    error_message text,
    params jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX scrape_jobs_org_status_idx ON scrape_jobs(org_id, status);
CREATE INDEX scrape_jobs_queue_idx ON scrape_jobs(status, requested_at);

-- One bill = one row = one PDF (pdf_object_key), per plan. No bill_lines /
-- bill_attachments tables.
CREATE TABLE bills (
    id bigserial PRIMARY KEY,
    org_id bigint NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    utility_account_id bigint REFERENCES utility_accounts(id) ON DELETE SET NULL,
    provider_id bigint REFERENCES providers(id),
    -- Direct property link so manually uploaded bills (no utility account)
    -- still roll up under a property.
    property_id bigint REFERENCES properties(id) ON DELETE SET NULL,
    -- Display vendor: provider display_name for scraped bills, AI-extracted
    -- vendor_raw for uploads.
    vendor_name text,
    amount_cents bigint NOT NULL CHECK (amount_cents >= 0),
    statement_date date,
    due_date date,
    service_start date,
    service_end date,
    -- 'overdue' is derived at read time (status='outstanding' AND due_date < today).
    status text NOT NULL DEFAULT 'outstanding' CHECK (status IN ('outstanding', 'paid')),
    source text NOT NULL DEFAULT 'upload' CHECK (source IN ('scrape', 'upload')),
    pdf_object_key text NOT NULL,
    sha256_pdf text,
    statement_id text,
    parse_confidence integer,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX bills_org_idx ON bills(org_id, created_at DESC);
CREATE INDEX bills_property_idx ON bills(property_id);
-- Scrape dedup: pulling the same statement twice for the same account is the
-- "no new bill posted yet" signal the retry/backoff logic keys off.
CREATE UNIQUE INDEX bills_scrape_dedup
    ON bills(utility_account_id, statement_date, amount_cents)
    WHERE source = 'scrape';
