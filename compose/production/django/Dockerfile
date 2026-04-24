# =============================================================================
# Validibot Production Dockerfile (multi-stage)
#
# Three-stage build to minimize image size and attack surface:
#   Stage 0 (extra-packages): Provides an empty default overlay directory
#                             so commercial builds can inject packages
#                             without branching the Dockerfile
#   Stage 1 (builder):        Installs compilers and builds Python C
#                             extensions
#   Stage 2 (runtime):        Slim image with only runtime dependencies
#
# The builder stage adds ~600MB of build tools (gcc, headers, git) that
# are thrown away. The runtime stage only keeps what's needed to serve
# the Django application.
#
# The same Dockerfile is used by community and hosted builds. Hosted
# builds layer on the ``cloud`` extra + Pro/Cloud packages through two
# mechanisms:
#   - EXTRA_UV_EXTRAS build arg (space-separated pyproject.toml extras)
#   - --build-context extra-packages=./_extra_packages (overlay dir)
# See ``just/gcp/mod.just`` for the call site when
# ``GCP_BUILD_VALIDIBOT_CLOUD=1``; self-hosters building via
# docker-compose leave both at their defaults for a community build.
# =============================================================================


# =============================================================================
# Stage 0: Empty defaults
#
# A tiny stage whose only job is to own an empty ``/_extra_packages``
# directory. The builder stage below copies from this by default, so
# community builds do not need to create ``_extra_packages/`` in the
# build context. Hosted / commercial builds override this with
# ``--build-context extra-packages=./_extra_packages`` at ``just gcp
# build`` time to inject the actual packages.
#
# This pattern (empty default stage + optional overlay via
# ``--build-context``) is BuildKit's idiomatic solution to the "COPY
# may or may not have a source" problem: no conditional COPY, no
# multi-file Dockerfile drift, works identically for every build path.
# =============================================================================
FROM alpine:3.20 AS extra-packages
RUN mkdir -p /_extra_packages


# =============================================================================
# Stage 1: Builder
#
# Purpose: Compile Python packages that have C extensions (psycopg, lxml,
# cryptography, pillow, grpcio, argon2-cffi, etc.). The resulting .venv
# is copied to the runtime stage; everything else is discarded.
# =============================================================================
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=1 \
    UV_CACHE_DIR=/tmp/uv-cache

ARG APP_HOME=/app
ARG VALIDIBOT_COMMERCIAL_PACKAGE=""
ARG VALIDIBOT_PRIVATE_INDEX_URL=""

# Extra uv extras passed through to ``uv sync`` (comma-separated, or
# empty). Used by cloud/enterprise builds to pull in provider-specific
# dependency groups (e.g. ``cloud`` enables the Stripe extra).
# Self-hosters leave this blank.
ARG EXTRA_UV_EXTRAS=""

# Optional commercial-package installation. When a deployment wants to
# layer validibot-pro, validibot-enterprise, or validibot-cloud packages
# on top of the community venv, it places the source trees as
# subdirectories of ``_extra_packages/`` in the build context *before*
# invoking ``docker build``. Every subdirectory is installed with
# ``uv pip install --no-deps`` in the order ``find`` returns them, so
# packages that register features should be named to sort before
# packages that depend on those features (e.g. ``validibot-pro/`` sorts
# before ``validibot-cloud/`` alphabetically).
#
# The directory is required to exist but may be empty — the RUN block
# below no-ops when it has no subdirectories. This keeps the same
# Dockerfile usable for community-only deploys and commercial builds
# without a conditional COPY (which Docker handles poorly).
WORKDIR ${APP_HOME}

# Build tools — only needed here, not in the final image.
#   build-essential: C/C++ compiler for Python extensions
#   libpq-dev:      PostgreSQL headers for psycopg[c]
#   gettext:        Django translation compilation (compilemessages)
#   git:            Needed for django-celery-beat installed from Git
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gettext \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files first for Docker layer caching.
# Changes to application code won't invalidate the dependency layer.
COPY pyproject.toml uv.lock ${APP_HOME}/
COPY --chmod=755 compose/common/install-commercial-package.sh /usr/local/bin/install-commercial-package

# Install production dependencies (no dev extras).
#   --frozen: Use the lockfile exactly as resolved (no re-resolution,
#     so [tool.uv.sources] overrides are not consulted)
#   --no-install-project: Don't install validibot itself (it's a Django
#     project run from the working directory, not a package)
#
# EXTRA_UV_EXTRAS is a space-separated list of pyproject.toml extras
# to include (e.g. "cloud" enables the Stripe extra for hosted
# deployments). Split into repeated --extra flags so that empty/blank
# → zero flags, "cloud" → --extra cloud, "cloud enterprise" →
# --extra cloud --extra enterprise.
RUN EXTRA_FLAGS=""; \
    for extra in ${EXTRA_UV_EXTRAS}; do EXTRA_FLAGS="${EXTRA_FLAGS} --extra ${extra}"; done; \
    uv sync --no-dev --no-install-project --frozen ${EXTRA_FLAGS} \
    && install-commercial-package ${APP_HOME}/.venv/bin/python \
    && rm -rf /tmp/uv-cache

# Install optional commercial layers packaged as directories under
# ``_extra_packages/``. Each subdirectory is installed with
# ``uv pip install --no-deps`` (its dependencies are already in the
# venv from ``uv sync`` above). An empty directory is a clean no-op,
# so community builds pass through untouched.
#
# ``COPY --from=extra-packages`` pulls from the empty Stage 0 by
# default. Hosted builds override it with
# ``--build-context extra-packages=./_extra_packages`` on the
# ``docker build`` command line so the overlay directory contains
# the real packages instead of an empty one. See
# ``just/gcp/mod.just`` for the call site.
COPY --from=extra-packages /_extra_packages /tmp/_extra_packages
RUN set -e; \
    if [ -n "$(ls -A /tmp/_extra_packages 2>/dev/null)" ]; then \
        for pkg_dir in $(find /tmp/_extra_packages -mindepth 1 -maxdepth 1 -type d | sort); do \
            echo "Installing extra commercial package from $(basename "$pkg_dir")..."; \
            uv pip install --no-deps "$pkg_dir"; \
        done; \
    fi; \
    rm -rf /tmp/_extra_packages


# =============================================================================
# Stage 2: Runtime
#
# Purpose: Minimal image that runs the Django application. No compilers,
# no dev headers, no git — just the Python runtime, compiled extensions
# (from the builder), and the application code.
# =============================================================================
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG APP_HOME=/app
WORKDIR ${APP_HOME}

# Create the django user BEFORE copying files so we can use --chown on COPY.
# This avoids a separate `chown -R` layer that would duplicate all file data.
# The docker group is for Docker socket access when running validator
# containers locally (entrypoint.sh adjusts the GID to match the host).
# We also make django own /app (WORKDIR creates it as root) and pre-create
# the staticfiles directory so collectstatic can write to it at startup.
RUN addgroup --system django \
  && adduser --system --ingroup django django \
  && (getent group docker || groupadd docker) \
  && usermod -aG docker django \
  && chown django:django ${APP_HOME} \
  && mkdir -p ${APP_HOME}/staticfiles && chown django:django ${APP_HOME}/staticfiles

# Runtime-only system packages — no compilers, no headers.
#   libpq5:            PostgreSQL client library (psycopg runtime dependency)
#   postgresql-client: pg_isready command (Docker Compose DB wait in entrypoint)
#   curl:              Health checks in Docker Compose deployments
#   gosu:              Privilege drop from root to django in entrypoint.sh
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-built virtual environment from the builder.
# This contains all compiled C extensions (psycopg, lxml, etc.) without
# needing the compilers that built them.
COPY --from=builder --chown=django:django ${APP_HOME}/.venv ${APP_HOME}/.venv

# Copy the application code, owned by django user.
# Using --chown avoids a separate RUN chown layer that would duplicate data.
COPY --chown=django:django . ${APP_HOME}/

# Copy entrypoint/start scripts (these run as root initially for Docker
# socket permissions, then drop to django via gosu).
COPY compose/production/django/entrypoint.sh /entrypoint
COPY compose/production/django/start.sh /start
RUN chmod +x /entrypoint /start

# Put the venv's bin directory first so `python`, `gunicorn`, etc.
# resolve to the venv versions.
ENV PATH="${APP_HOME}/.venv/bin:${PATH}"

# Entrypoint runs as root to fix Docker socket permissions (stat, chgrp,
# groupmod), then drops to the django user via gosu. See entrypoint.sh.
USER root

ENTRYPOINT ["/entrypoint"]
CMD ["/start"]
