#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

# Only the main django container runs migrations
# Other containers (worker, celery) skip this to avoid race conditions
python manage.py migrate --noinput

python manage.py runserver 0.0.0.0:8000
