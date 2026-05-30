#!/usr/bin/env bash
# Initialise the mimic_creditline database: create it, apply the schema, create
# the least-privilege agent role, then seed. Uses the ADMIN_* connection; the
# agent role is never used here.
set -euo pipefail

cd "$(dirname "$0")/.."

HOST="${ADMIN_DB_HOST:-localhost}"
PORT="${ADMIN_DB_PORT:-5433}"
USER="${ADMIN_DB_USER:-postgres}"
export PGPASSWORD="${ADMIN_DB_PASSWORD:-postgres}"

psql_admin() { psql -h "$HOST" -p "$PORT" -U "$USER" "$@"; }

echo "==> creating database mimic_creditline (if absent)"
psql_admin -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='mimic_creditline'" \
  | grep -q 1 || psql_admin -d postgres -c "CREATE DATABASE mimic_creditline"

echo "==> applying schema"
psql_admin -d mimic_creditline -v ON_ERROR_STOP=1 -f db/01_mimic_creditline_schema.sql

echo "==> creating role + grants"
psql_admin -d mimic_creditline -v ON_ERROR_STOP=1 -f db/03_roles_and_grants.sql

echo "==> seeding data"
python -m db.seed.seed_mimic

echo "==> done."
