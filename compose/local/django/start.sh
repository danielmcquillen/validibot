#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

# Only the main web container runs migrations
# Other containers (worker, scheduler) skip this to avoid race conditions
python manage.py migrate --noinput

# First-run setup: Initialize Validibot if this is a fresh installation.
# Checks if roles exist (created by setup_validibot) to detect first run.
# The command is idempotent, so it's safe to run even if already configured.
if ! python manage.py shell -c "from validibot.users.models import Role; exit(0 if Role.objects.exists() else 1)" 2>/dev/null; then
  echo "First run detected - running initial setup..."
  python manage.py setup_validibot --noinput
else
  # Sync system validators on every startup to ensure catalog entries are current.
  # This is fast (idempotent) and ensures EnergyPlus/FMU/THERM signals are available.
  python manage.py sync_validators
fi

python manage.py runserver 0.0.0.0:8000
