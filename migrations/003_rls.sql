-- 003_rls.sql — tenant isolation as defense-in-depth (Postgres row-level
-- security), on top of the existing application-layer `WHERE org_id = $1`.
--
-- How it works: the app sets a per-connection GUC `app.org_id`.
--   * a numeric org id  → that connection may only see/write that org's rows
--   * 'bypass'          → trusted background/admin paths (worker, scheduler,
--                         login, migrations, CLI) — full access
--   * unset/empty       → deny everything (fail-safe)
--
-- IMPORTANT: RLS is NOT enforced for superusers or for a role with BYPASSRLS,
-- and FORCE is required so it applies even to the table owner. On managed hosts
-- (Render) the app connects as a non-superuser owner, so this enforces. Against
-- a local superuser it is a no-op — test with a dedicated non-superuser role.

CREATE OR REPLACE FUNCTION app_current_org() RETURNS bigint
LANGUAGE sql STABLE AS $$
  SELECT CASE
    WHEN coalesce(current_setting('app.org_id', true), '') IN ('', 'bypass') THEN NULL
    ELSE current_setting('app.org_id', true)::bigint
  END
$$;

CREATE OR REPLACE FUNCTION app_is_bypass() RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT coalesce(current_setting('app.org_id', true), '') = 'bypass'
$$;

-- organizations is keyed on `id`; the rest carry `org_id`.
DO $$
DECLARE
  t text;
  keycol text;
BEGIN
  FOR t, keycol IN
    SELECT * FROM (VALUES
      ('organizations', 'id'),
      ('org_users', 'org_id'),
      ('properties', 'org_id'),
      ('utility_accounts', 'org_id'),
      ('scrape_jobs', 'org_id'),
      ('bills', 'org_id')
    ) AS v(t, keycol)
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS org_isolation ON %I', t);
    EXECUTE format(
      'CREATE POLICY org_isolation ON %I '
      || 'USING (app_is_bypass() OR %I = app_current_org()) '
      || 'WITH CHECK (app_is_bypass() OR %I = app_current_org())',
      t, keycol, keycol);
  END LOOP;
END $$;
