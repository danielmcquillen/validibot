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
    # gosu for dropping privileges after Docker socket setup
    gosu \
    && rm -rf /var/lib/apt/lists/*


# Copy dependency files first for better caching
COPY pyproject.toml uv.lock ${APP_HOME}/

# Install dependencies with dev group for local work
# --no-install-project skips installing validibot itself (it's not a package, just a Django project)
RUN uv sync --group dev --frozen --no-install-project

# Copy the rest of the project
COPY . ${APP_HOME}/

# Create non-root user with a real home and own the app directory
# Also create a docker group - the entrypoint will adjust the GID to match the host socket
RUN addgroup --system django \
  && adduser --system --home /home/django --shell /bin/bash --ingroup django django \
  && mkdir -p /home/django \
  && chown -R django:django ${APP_HOME} /home/django \
  && (getent group docker || groupadd docker) \
  && usermod -aG docker django

# Copy entrypoint/start scripts and make executable
USER root
COPY compose/local/django/entrypoint.sh /entrypoint
COPY compose/local/django/start.sh /start
COPY compose/local/django/start-worker.sh /start-worker
RUN chmod +x /entrypoint /start /start-worker

# Devcontainer-style user (optional, useful for VS Code Remote)
# Use flexible GID/UID creation to handle cases where 1000 already exists
RUN (getent group 1000 || groupadd --gid 1000 dev-user) \
  && (id -u 1000 &>/dev/null || useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home dev-user) \
  && echo dev-user ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/dev-user \
  && chmod 0440 /etc/sudoers.d/dev-user

# Place executables in the environment at the front of the path
ENV PATH="${APP_HOME}/.venv/bin:${PATH}"
ENV PYTHONPATH="${APP_HOME}/.venv/lib/python3.13/site-packages"

# Entrypoint runs as root to fix Docker socket permissions, then drops to django via gosu
USER root

# Use entrypoint (waits for DB, fixes docker perms, drops to django) and default to /start
ENTRYPOINT ["/entrypoint"]
