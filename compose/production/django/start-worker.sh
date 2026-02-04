#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

uv run python manage.py migrate --noinput
GUNICORN_TIMEOUT_SECONDS="${GUNICORN_TIMEOUT_SECONDS:-3600}"

uv run gunicorn validibot.wsgi:application \
  --bind 0.0.0.0:8001 \
  --timeout "${GUNICORN_TIMEOUT_SECONDS}"
