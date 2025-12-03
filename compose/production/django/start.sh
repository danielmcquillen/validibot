#!/bin/bash
# Exit on any command failure.
set -o errexit
# Treat failures in pipelines as errors.
set -o pipefail
# Error on unset variables to catch misconfigurations early.
set -o nounset

# Load environment variables from secrets file if it exists (Cloud Run)
if [ -f /secrets/.env ]; then
  # Let sourced variables be exported automatically.
  echo "Loading environment from /secrets/.env..."
  # Export variables read from the secrets file.
  set -a
  source /secrets/.env
  # Stop automatically exporting new variables.
  set +a
fi

# Run database migrations without prompting for input.
python manage.py migrate --noinput
# Collect static assets for serving.
python manage.py collectstatic --noinput
# Launch Gunicorn to serve the Django application.
gunicorn config.wsgi:application --bind 0.0.0.0:8000
