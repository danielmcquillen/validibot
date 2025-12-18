# Load local development environment variables.
#
# Usage:
#   source set-env.sh
#
# This exports all variables from .envs/.local/.django so they're available
# to Django commands run directly in the terminal (outside Docker).

set -o allexport
source .envs/.local/.django
set +o allexport
