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
  # This is fast (idempotent) and ensures all step I/O definitions are available.
  #
  # ``--allow-drift`` is the LOCAL stance: a developer iterating on a
  # validator config naturally changes the semantic digest, and blocking
  # container startup on every config edit makes the dev loop unusable.
  # The production start script (compose/production/django/start.sh) keeps
  # drift detection strict — that's where catching an un-bumped config
  # version actually matters, because workflows pinned to (slug, version)
  # would silently change behavior under load.
  python manage.py sync_validators --allow-drift
fi

# Keep code-backed local data current on every startup. Both commands are
# idempotent, so a fresh database and an existing development database follow
# the same path. These remain local-only: production startup does not seed
# bundled development resources.
echo "Syncing local help pages..."
python manage.py sync_help

echo "Ensuring local EnergyPlus weather resources..."
python manage.py seed_weather_files

python manage.py runserver 0.0.0.0:8000
