#!/usr/bin/env bash
# Refresh the local (Docker) database from the hosted Supabase project.
#
# The cloud project is the main copy; the laptop is a working copy. This script WIPES the local
# database (`supabase db reset`), re-applies the migrations, and loads the cloud's public-schema data.
# Schema always flows the other way: laptop -> cloud via `supabase db push --linked`.
#
# Usage: scripts/pull_cloud.sh --yes
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "${1:-}" != "--yes" ]]; then
  echo "This wipes the LOCAL database and replaces its data with the cloud copy."
  echo "Run again with --yes to continue."
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env.cloud
set +a
: "${DATABASE_URL_CLOUD:?DATABASE_URL_CLOUD missing from .env.cloud}"

LOCAL_URL="postgresql://postgres:postgres@127.0.0.1:54322/postgres"
DUMP="data/cloud-dump.sql"
mkdir -p data

echo "Dumping cloud data to $DUMP ..."
pg_dump "$DATABASE_URL_CLOUD" --data-only --schema=public --no-owner --no-privileges -f "$DUMP"

echo "Resetting local database (migrations only, no data) ..."
supabase db reset --local

echo "Loading cloud data into local ..."
# replica mode skips triggers (revision history, FK checks) while rows are copied as-is.
PGOPTIONS="-c session_replication_role=replica" psql -X -q -v ON_ERROR_STOP=1 "$LOCAL_URL" -f "$DUMP"

echo "Done. Local now matches the cloud copy:"
uv run dm status
