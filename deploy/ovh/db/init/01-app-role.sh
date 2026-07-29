#!/bin/bash
# Runs once, on first cluster init (empty pgdata), as the Postgres superuser.
# Creates the NON-superuser application role. This is the whole reason RLS
# (003_rls.sql) enforces: the app must NOT connect as a superuser (superusers
# and BYPASSRLS roles skip row-level security). Migrations run as this role, so
# the tables are owned by it — 003_rls.sql uses FORCE RLS, which applies even to
# the owner, with the 'bypass' GUC sentinel for the trusted worker/login pool.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE ROLE metered_app WITH LOGIN PASSWORD '${APP_DB_PASSWORD}' NOSUPERUSER NOBYPASSRLS NOCREATEROLE;
	ALTER DATABASE ${POSTGRES_DB} OWNER TO metered_app;
	ALTER SCHEMA public OWNER TO metered_app;
	GRANT ALL ON SCHEMA public TO metered_app;
EOSQL

echo "[init] created non-superuser role 'metered_app' (RLS will enforce)"
