FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=1 \
    PATH="/root/.local/bin:${PATH}"

ARG APP_HOME=/app

WORKDIR ${APP_HOME}

# System deps for psycopg and tooling
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    # Translations dependencies
    gettext \
    # entrypoint
    wait-for-it \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*


# Copy dependency files first for better caching
COPY pyproject.toml uv.lock ${APP_HOME}/

# Install dependencies (no dev extras) for production
RUN uv sync --frozen

# Copy the rest of the project
COPY . ${APP_HOME}/

# Create non-root user and own the app directory
RUN addgroup --system django && adduser --system --ingroup django django && chown -R django:django ${APP_HOME}
USER django

# Copy entrypoint/start scripts and make executable
USER root
COPY compose/production/django/entrypoint.sh /entrypoint
COPY compose/production/django/start.sh /start
COPY compose/production/django/start-worker.sh /start-worker
RUN chmod +x /entrypoint /start /start-worker && chown django:django /entrypoint /start /start-worker

# Drop back to non-root
USER django

# Place executables in the environment at the front of the path
ENV PATH="${APP_HOME}/.venv/bin:${PATH}"

# Use entrypoint (waits for DB); compose sets the command (/start or /start-worker)
ENTRYPOINT ["/entrypoint"]
