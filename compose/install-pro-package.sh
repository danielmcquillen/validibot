#!/bin/bash
# Editable-install validibot-pro into the venv at container boot.
#
# Used by docker-compose.local-pro.yml's ``web``/``worker``/``scheduler``
# command wrappers. The ../validibot-pro directory is volume-mounted into
# the container at /app/validibot-pro; this script registers it as an
# editable install so source edits in the sibling repo are picked up
# immediately without a rebuild.
#
# Pro's runtime dependencies (authlib, cryptography) are already in the
# community venv from ``uv sync`` at image build time, so --no-deps is
# safe and keeps this step fast.
#
# If you need the cloud stack too (billing, metering, tenancy), use
# docker-compose.cloud.yml in validibot-cloud/ instead — that runs
# install-cloud-packages.sh which adds both pro and cloud.
#
# Runs as the 'django' user (after the entrypoint drops privileges).

set -e

# Idempotency: if validibot_pro imports, we already registered the package.
if ! python -c "import validibot_pro" 2>/dev/null; then
    echo "Registering validibot-pro (editable install)..."
    uv pip install --no-deps -e /app/validibot-pro
    echo "validibot-pro ready."
else
    echo "validibot-pro already installed."
fi

exec "$@"
