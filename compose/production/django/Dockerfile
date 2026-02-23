# =============================================================================
# Validibot Production Dockerfile (multi-stage)
#
# Two-stage build to minimize image size and attack surface:
#   Stage 1 (builder): Installs compilers and builds Python C extensions
#   Stage 2 (runtime): Slim image with only runtime dependencies
#
# The builder stage adds ~600MB of build tools (gcc, headers, git) that
# are thrown away. The runtime stage only keeps what's needed to serve
# the Django application.
# =============================================================================


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

# Install production dependencies (no dev extras).
#   --no-sources: Skip [tool.uv.sources] overrides (local dev paths)
#   --no-install-project: Don't install validibot itself (it's a Django
#     project run from the working directory, not a package)
RUN uv sync --no-dev --no-sources --no-install-project \
    && rm -rf /tmp/uv-cache

# -- Cloud layer (optional) ---------------------------------------------------
# When BUILD_CLOUD=1, install the proprietary validibot-cloud package into the
# venv.  For community builds the glob matches nothing and the conditional
# skips, so this block is a complete no-op.
ARG BUILD_CLOUD=0
COPY _validibot-clou[d]/ /tmp/_validibot-cloud/
RUN if [ "$BUILD_CLOUD" = "1" ] && [ -f /tmp/_validibot-cloud/pyproject.toml ]; then \
        uv pip install --no-deps /tmp/_validibot-cloud/ \
        && uv pip install stripe "google-cloud-tasks>=2.20.0" "google-cloud-run>=0.13.0" "google-cloud-storage>=2.14.0" \
        && rm -rf /tmp/_validibot-cloud; \
    fi
# ------------------------------------------------------------------------------


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
