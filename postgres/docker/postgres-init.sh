#!/usr/bin/env bash
# Run all migrations in numerical order on first container init.
# Idempotent (migrations use CREATE ... IF NOT EXISTS).

set -euo pipefail

MIG_DIR=/docker-entrypoint-initdb.d/migrations

echo "[init] applying migrations from ${MIG_DIR}"
for f in $(ls "${MIG_DIR}"/*.sql | sort); do
    echo "[init] ▶ $(basename "${f}")"
    psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" \
         --dbname "${POSTGRES_DB:-postgres}" -f "${f}"
done
echo "[init] migrations done."
