#!/bin/bash
# Exit on any command failure.
set -o errexit
# Treat failures in pipelines as errors.
set -o pipefail
# Error on unset variables to catch misconfigurations early.
set -o nounset

# Note: Environment variables are loaded by the entrypoint script (/entrypoint)

# Run database migrations without prompting for input.
python manage.py migrate --noinput
# Collect static assets for serving.
python manage.py collectstatic --noinput
# Launch Gunicorn to serve the Django application.
gunicorn config.wsgi:application --bind 0.0.0.0:8000
