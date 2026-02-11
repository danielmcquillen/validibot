FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=1 \
    UV_CACHE_DIR=/tmp/uv-cache \
    PATH="/root/.local/bin:${PATH}"

ARG APP_HOME=/app

WORKDIR ${APP_HOME}

# System deps for psycopg and tooling
RUN apt-get update && apt-get install -y --no-install-recommends \
    # C compiler for Python extensions (psycopg, etc.)
    build-essential \
    # PostgreSQL client library for psycopg
    libpq-dev \
    # Django translation compilation (compilemessages)
    gettext \
    # Git for installing vb-shared from GitHub
    git \
    # curl for healthchecks
    curl \
    # gosu for dropping privileges after Docker socket setup
    gosu \
    && rm -rf /var/lib/apt/lists/*


# Copy dependency files first for better caching
COPY pyproject.toml uv.lock ${APP_HOME}/

# Install dependencies (no dev extras) for production
# --no-sources skips [tool.uv.sources] overrides (which point to local dev paths)
# --no-install-project skips installing validibot itself (it's not a package, just a Django project)
RUN uv sync --no-dev --no-sources --no-install-project

# Copy the rest of the project
COPY . ${APP_HOME}/

# Create non-root user and own the app directory
# Also create docker group for socket access - entrypoint will adjust GID to match host
RUN addgroup --system django \
  && adduser --system --ingroup django django \
  && chown -R django:django ${APP_HOME} \
  && (getent group docker || groupadd docker) \
  && usermod -aG docker django

# Copy entrypoint/start scripts and make executable
USER root
COPY compose/production/django/entrypoint.sh /entrypoint
COPY compose/production/django/start.sh /start
RUN chmod +x /entrypoint /start

# Place executables in the environment at the front of the path
ENV PATH="${APP_HOME}/.venv/bin:${PATH}"

# Entrypoint runs as root to fix Docker socket permissions, then drops to django via gosu
USER root

# Use entrypoint (waits for DB, fixes docker perms, drops to django)
ENTRYPOINT ["/entrypoint"]

# Default command - run the web server
CMD ["/start"]
