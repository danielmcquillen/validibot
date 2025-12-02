#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

uv run python manage.py migrate --noinput
uv run python manage.py runserver 0.0.0.0:8000
