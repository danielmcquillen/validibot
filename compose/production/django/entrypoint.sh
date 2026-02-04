#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

# Load environment variables from secrets file if it exists (Cloud Run)
if [ -f /secrets/.env ]; then
  echo "Loading environment from /secrets/.env..."
  set -a
  source /secrets/.env
  set +a
fi

# Cloud SQL uses Unix sockets, Docker Compose uses TCP
# Check if we're using Cloud SQL (socket path exists or CLOUD_SQL_CONNECTION_NAME is set)
if [ -n "${CLOUD_SQL_CONNECTION_NAME:-}" ] || [ -d "/cloudsql" ]; then
  echo "Cloud SQL detected, skipping TCP wait (using Unix socket)..."
else
  postgres_host="${POSTGRES_HOST:-postgres}"
  postgres_port="${POSTGRES_PORT:-5432}"
  postgres_user="${POSTGRES_USER:-validibot}"

  echo "Waiting for Postgres at ${postgres_host}:${postgres_port}..."
  until pg_isready -h "${postgres_host}" -p "${postgres_port}" -U "${postgres_user}" >/dev/null 2>&1; do
    sleep 1
  done
fi

exec "$@"
