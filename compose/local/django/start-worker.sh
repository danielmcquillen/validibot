#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

# Worker does NOT run migrations - the main django container handles that
# This prevents race conditions when multiple containers start simultaneously

python manage.py runserver 0.0.0.0:8001
