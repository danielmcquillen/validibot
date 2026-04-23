# =============================================================================
# Validibot community + Pro — local Docker Compose
# =============================================================================
#
# Run the community stack locally with validibot-pro editable-installed
# from ../validibot-pro. Use this when you want Pro features (teams,
# guests, MCP) without the full cloud layer on top.
#
# Layered on docker-compose.local.yml via Compose's multi-file merge:
#   docker compose -f docker-compose.local.yml \
#                  -f docker-compose.local-pro.yml \
#                  up -d
#
# Usage:
#   just local-pro up                            # start community + pro
#   just local-pro up --build                    # rebuild + start
#   ENABLE_MCP_SERVER=true just local-pro up     # + MCP container
#   just local-pro down                          # stop (tears down MCP too)
#   just local-pro rebuild                       # full clean rebuild
#
# If you want the cloud layer (billing, tenancy, onboarding), use
# ``just local-cloud up`` instead — that runs docker-compose.cloud.yml
# from the validibot-cloud repo.
#
# =============================================================================

# Base compose + overlay. Paths are relative to the community repo root.
base_compose := "docker-compose.local.yml"
overlay_compose := "docker-compose.local-pro.yml"

# The same _db_url helper used by just/local.just — duplicated here
# because just modules have isolated scope (can't reference parent recipes).
_db_url := 'DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"'

# Read the base module's optional .envs/.local/.build file if present,
# so VALIDIBOT_COMMERCIAL_PACKAGE-style build args still flow through.
# We don't use them for local-pro (we editable-install from the sibling
# directory instead of a private PyPI), but the base compose's build args
# would fail if the file were missing.
compose_env_args := `bash -lc 'if [ -f ".envs/.local/.build" ]; then printf -- "--env-file .envs/.local/.build"; fi'`


# Start community + pro stack (detached).
#
# Recipe-level knobs (ENABLE_MCP_SERVER, future flags) come from
# ``.envs/.local/.build`` — see ``.envs.example/.local/.build`` for the
# variable reference. Setting a knob inline (``ENABLE_MCP_SERVER=true
# just local-pro up``) still works and wins over the file.
#
# Rebuilds the venv volume when --build is passed so new dependencies
# from pro (authlib, cryptography) are picked up cleanly — mirrors the
# behaviour of ``just local-cloud up --build``.
[no-cd]
up *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # Load shell-level knobs from .envs/.local/.build if present. ``set -a``
    # exports every variable the file defines, so downstream checks like
    # ``${ENABLE_MCP_SERVER:-false}`` see them. Absent file → no-op.
    if [ -f .envs/.local/.build ]; then
        set -a
        # shellcheck disable=SC1091
        source .envs/.local/.build
        set +a
    fi
    args="{{args}}"
    if [[ "$args" == *"--build"* ]]; then
        docker volume rm validibot_validibot_local_venv 2>/dev/null || true
    fi
    if [[ "${ENABLE_MCP_SERVER:-false}" == "true" || \
          "${ENABLE_MCP_SERVER:-false}" == "1" || \
          "${ENABLE_MCP_SERVER:-false}" == "yes" ]]; then
        export COMPOSE_PROFILES="${COMPOSE_PROFILES:+${COMPOSE_PROFILES},}mcp"
        echo "ENABLE_MCP_SERVER is set — including the MCP container (profile: mcp)"
    fi
    docker compose {{compose_env_args}} \
        -f "{{base_compose}}" \
        -f "{{overlay_compose}}" \
        up -d $args


# Stop the community + pro stack. Tears down every service including any
# MCP container regardless of how it was started.
[no-cd]
down:
    #!/usr/bin/env bash
    set -euo pipefail
    export COMPOSE_PROFILES="${COMPOSE_PROFILES:+${COMPOSE_PROFILES},}mcp"
    docker compose {{compose_env_args}} \
        -f "{{base_compose}}" \
        -f "{{overlay_compose}}" \
        down


# Rebuild and start, clearing the venv volume first.
#
# The venv lives in a named volume that persists across rebuilds; when
# pro's deps change we need to flush it so uv pip install sees the fresh
# image layer.
[no-cd]
rebuild *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # Same .envs/.local/.build sourcing as ``up`` so recipe knobs are
    # consistent across a build loop.
    if [ -f .envs/.local/.build ]; then
        set -a
        # shellcheck disable=SC1091
        source .envs/.local/.build
        set +a
    fi
    if [[ "${ENABLE_MCP_SERVER:-false}" == "true" || \
          "${ENABLE_MCP_SERVER:-false}" == "1" || \
          "${ENABLE_MCP_SERVER:-false}" == "yes" ]]; then
        export COMPOSE_PROFILES="${COMPOSE_PROFILES:+${COMPOSE_PROFILES},}mcp"
    fi
    docker compose {{compose_env_args}} \
        -f "{{base_compose}}" \
        -f "{{overlay_compose}}" \
        down
    docker volume rm validibot_validibot_local_venv 2>/dev/null || true
    docker compose {{compose_env_args}} \
        -f "{{base_compose}}" \
        -f "{{overlay_compose}}" \
        build {{args}}
    docker compose {{compose_env_args}} \
        -f "{{base_compose}}" \
        -f "{{overlay_compose}}" \
        up -d
