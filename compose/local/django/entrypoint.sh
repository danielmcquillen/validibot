#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

# Construct DATABASE_URL from individual postgres variables
# This makes .postgres the single source of truth for credentials
if [ -z "${DATABASE_URL:-}" ]; then
  export DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
fi

postgres_host="${POSTGRES_HOST:-postgres}"
postgres_port="${POSTGRES_PORT:-5432}"
postgres_user="${POSTGRES_USER:-validibot}"

echo "Waiting for Postgres at ${postgres_host}:${postgres_port}..."
until pg_isready -h "${postgres_host}" -p "${postgres_port}" -U "${postgres_user}" >/dev/null 2>&1; do
  sleep 1
done

# Fix Docker socket permissions if mounted (for spawning validator containers)
# The docker group GID in the container may not match the host socket's GID
if [ -S /var/run/docker.sock ]; then
  DOCKER_SOCK_GID=$(stat -c '%g' /var/run/docker.sock)
  CURRENT_DOCKER_GID=$(getent group docker | cut -d: -f3)
  if [ "${CURRENT_DOCKER_GID}" != "${DOCKER_SOCK_GID}" ]; then
    # Modify docker group to match socket GID
    groupmod -g "${DOCKER_SOCK_GID}" docker 2>/dev/null || true
  fi
fi

# Drop privileges and run command as django user
exec gosu django "$@"
