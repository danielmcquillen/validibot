#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

postgres_host="${POSTGRES_HOST:-postgres}"
postgres_port="${POSTGRES_PORT:-5432}"
postgres_user="${POSTGRES_USER:-validibot}"

echo "Waiting for Postgres at ${postgres_host}:${postgres_port}..."
until pg_isready -h "${postgres_host}" -p "${postgres_port}" -U "${postgres_user}" >/dev/null 2>&1; do
  sleep 1
done

exec "$@"
