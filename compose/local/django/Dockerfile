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
    git \
    chromium \
    chromium-driver \
    fonts-liberation \
    # Translations dependencies
    gettext \
    # entrypoint
    wait-for-it \
    postgresql-client \
    curl \
    sudo \
    && rm -rf /var/lib/apt/lists/*


# Copy dependency files first for better caching
COPY pyproject.toml uv.lock ${APP_HOME}/

# Create placeholder for vb_shared_dev (will be volume-mounted at runtime)
# This allows uv sync to complete during build
RUN mkdir -p ${APP_HOME}/vb_shared_dev && \
    echo '[project]' > ${APP_HOME}/vb_shared_dev/pyproject.toml && \
    echo 'name = "vb-shared"' >> ${APP_HOME}/vb_shared_dev/pyproject.toml && \
    echo 'version = "0.1.0"' >> ${APP_HOME}/vb_shared_dev/pyproject.toml && \
    mkdir -p ${APP_HOME}/vb_shared_dev/vb_shared && \
    touch ${APP_HOME}/vb_shared_dev/vb_shared/__init__.py

# Install dependencies with dev extras for local work
RUN uv sync --extra dev --frozen

# Copy the rest of the project
COPY . ${APP_HOME}/

# Create non-root user with a real home and own the app directory
RUN addgroup --system django \
  && adduser --system --home /home/django --shell /bin/bash --ingroup django django \
  && mkdir -p /home/django \
  && chown -R django:django ${APP_HOME} /home/django

# Copy entrypoint/start scripts and make executable
USER root
COPY compose/local/django/entrypoint.sh /entrypoint
COPY compose/local/django/start.sh /start
COPY compose/local/django/start-worker.sh /start-worker
RUN chmod +x /entrypoint /start /start-worker

# Devcontainer-style user (optional, useful for VS Code Remote)
RUN groupadd --gid 1000 dev-user \
  && useradd --uid 1000 --gid dev-user --shell /bin/bash --create-home dev-user \
  && echo dev-user ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/dev-user \
  && chmod 0440 /etc/sudoers.d/dev-user

# Place executables in the environment at the front of the path
ENV PATH="${APP_HOME}/.venv/bin:${PATH}"
ENV PYTHONPATH="${APP_HOME}/.venv/lib/python3.13/site-packages:${PYTHONPATH}"

# Drop to non-root (default app user)
USER django

# Use entrypoint (waits for DB) and default to /start (runserver)
ENTRYPOINT ["/entrypoint"]
