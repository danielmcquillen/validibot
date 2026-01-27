# ADR: Open-Core Self-Hosted Transformation

**Date:** 2026-01-28
**Status:** Proposed
**Author:** Daniel McQuillen

## Context

Validibot is currently architected as a hosted SaaS running on Google Cloud Platform, using Cloud Tasks for job queuing, Cloud Run Services for web/worker separation, and Cloud Run Jobs for async validators (EnergyPlus, FMI). While this architecture works well, it creates significant data liability concerns - we're responsible for storing and processing potentially sensitive building energy models and simulation data.

Following the successful models of projects like Sidekiq, GitLab, n8n, and Sentry, we want to transform Validibot into an open-core product where:

1. Users self-host the application on their own infrastructure
2. The core product is open-source under AGPL
3. Commercial features are available via yearly subscription with access to a private repository
4. We eliminate data liability by having users own their data

This ADR documents the comprehensive plan to transform the codebase from GCP-hosted SaaS to self-hosted open-core.

## Decision

We will transform Validibot into an open-core self-hosted product with the following structure:

### Licensing Model (Following Sidekiq)

| Tier          | License    | Distribution                       | Features                                                                            |
| ------------- | ---------- | ---------------------------------- | ----------------------------------------------------------------------------------- |
| **Community** | AGPL-3.0   | Public GitHub                      | Built-in validators, unlimited workflows/runs, single workspace, basic roles        |
| **Pro**       | Commercial | Private repo (yearly subscription) | Advanced validators, multiple workspaces, team management, priority support         |

**Note:** Enterprise tier (SSO/SAML, audit logs, advanced RBAC) is planned for future development but not part of the initial implementation. We'll focus on Community → Pro first.

**Subscription model:** Annual license fee grants access to private repository. License key validated at download/install time (not runtime), following Sidekiq's approach where paying customers get repository access.

### Terminology

| Term                    | Definition                                                                                            |
| ----------------------- | ----------------------------------------------------------------------------------------------------- |
| **Built-in validators** | Validators that run in the Django process (Basic, JSON Schema, XML Schema, AI)                        |
| **Advanced validators** | Validators packaged as self-contained Docker containers (EnergyPlus, FMI, and user-added containers) |

### Deployment Architecture

**Primary deployment method:** Docker Compose (with Podman recommended for security)

```
┌─────────────────────────────────────────────────────────────────┐
│                     Docker Compose Stack                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │   Web        │  │   Worker     │  │   Database   │          │
│  │   (Django)   │  │   (Django +  │  │  (PostgreSQL)│          │
│  │              │  │   Dramatiq)  │  │              │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│         │                 │                  │                   │
│         └────────────┬────┴──────────────────┘                  │
│                      │                                           │
│  ┌──────────────┐    │    ┌──────────────┬──────────────┐      │
│  │    Redis     │◄───┴───►│  File Store  │  Public Media │      │
│  │   (Broker)   │         │   (Volume)   │    Volume     │      │
│  └──────────────┘         └──────────────┴──────────────┘      │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Advanced Validators (Pro)                    │   │
│  │  ┌────────────┐ ┌────────────┐ ┌────────────┐           │   │
│  │  │ EnergyPlus │ │    FMI     │ │   Other    │           │   │
│  │  │ Container  │ │ Container  │ │ Containers │           │   │
│  │  └────────────┘ └────────────┘ └────────────┘           │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**Future:** Kubernetes/Helm chart (v2, based on user demand)

### Task Queue Selection: Dramatiq

After evaluating options, we'll use **Dramatiq** as the task queue:

| Option           | Pros                                         | Cons                                     |
| ---------------- | -------------------------------------------- | ---------------------------------------- |
| **Dramatiq**     | Simple, Redis-based, good defaults, reliable | Less ecosystem than Celery               |
| Celery           | Feature-rich, well-known                     | Complex, overkill for our needs          |
| Huey             | Very simple                                  | Limited features                         |
| Django 5.1 tasks | Built-in                                     | No retries, backoff, or queue management |

Dramatiq provides:

- Automatic retries with exponential backoff
- Priority queues
- Rate limiting
- Message middleware
- Simple Redis broker (same as caching)

### Container Orchestration: Wait Pattern

For spawning validator containers, we'll use the **synchronous wait pattern** (not callbacks):

```python
# Current GCP pattern (callback-based):
# 1. Worker triggers Cloud Run Job
# 2. Worker returns to pool
# 3. Job completes, POSTs callback
# 4. New worker handles callback

# New self-hosted pattern (wait-based):
# 1. Worker spawns Docker container
# 2. Worker waits for container completion
# 3. Worker processes result directly
# 4. Worker moves to next task
```

**Why wait pattern:**

- Simpler architecture (no callback endpoint needed)
- Container doesn't need network access to worker
- Easier to debug and reason about
- Result immediately available to same worker
- This is what Drone CI, GitLab Runner, and most CI/CD tools use

**Concurrency:** Handled by Dramatiq worker processes. More workers = more concurrent validations.

### Security Mitigations for Docker Socket

Mounting `/var/run/docker.sock` grants root-equivalent access. Mitigations:

1. **Recommend Podman** - Rootless by default, drop-in Docker replacement
2. **Resource limits** - Always set `mem_limit`, `cpu_quota`, `timeout`
3. **Network isolation** - Run validator containers with `network_mode='none'` unless needed
4. **Image allowlist** - Only system validators by default; custom validators require explicit approval
5. **Documentation** - Clear security documentation for self-hosters

```python
# Example container spawn with security limits
docker_client.containers.run(
    image=validator.container_image,
    environment={...},
    mem_limit='2g',
    cpu_quota=100000,  # 1 CPU
    network_mode='none',  # No network unless needed
    timeout=300,  # 5 minute hard limit
    remove=True,  # Auto-cleanup
)
```

## Feature Gating Strategy

### Community Edition (Free, AGPL)

| Feature                                   | Included |
| ----------------------------------------- | -------- |
| Built-in validators (Basic, JSON, XML, AI) | Yes      |
| Unlimited workflows                       | Yes      |
| Unlimited validation runs                 | Yes      |
| Single workspace                          | Yes      |
| Basic user roles (admin, member, viewer)  | Yes      |
| REST API access                           | Yes      |
| CLI tool                                  | Yes      |
| Community support (GitHub Discussions)    | Yes      |

### Pro Edition ($X/year per workspace)

| Feature                                   | Included |
| ----------------------------------------- | -------- |
| Everything in Community                   | Yes      |
| **Advanced validators** (EnergyPlus, FMI) | Yes      |
| User-added advanced validators            | Yes      |
| Multiple workspaces                       | Yes      |
| Team management (multiple teams per org)  | Yes      |
| Workspace-level roles                     | Yes      |
| Priority email support                    | Yes      |
| Private Docker registry access            | Yes      |

### Enterprise Edition (Future - Not in Initial Scope)

Enterprise features are planned for future development:

- SSO / SAML
- Audit logs
- Advanced RBAC (custom roles)
- Multi-environment (staging/production)
- Dedicated support + SLA

**We will focus on Community and Pro editions for the initial release.**

## Implementation Plan

### Phase 1: Abstract GCP Dependencies

**Goal:** Make GCP services optional/swappable without breaking existing deployments.

#### 1.1 Storage Abstraction

Create a storage backend interface that supports both GCS and local filesystem:

```python
# validibot/core/storage/base.py
class StorageBackend(ABC):
    @abstractmethod
    def upload(self, content: bytes, path: str) -> str:
        """Upload content, return URI."""
        pass

    @abstractmethod
    def download(self, uri: str) -> bytes:
        """Download content from URI."""
        pass

    @abstractmethod
    def delete(self, uri: str) -> None:
        """Delete content at URI."""
        pass

# validibot/core/storage/local.py
class LocalStorageBackend(StorageBackend):
    """File-system storage for self-hosted deployments."""

# validibot/core/storage/gcs.py
class GCSStorageBackend(StorageBackend):
    """Google Cloud Storage backend (existing code, refactored)."""
```

**Files to modify:**

- `validibot/validations/services/cloud_run/gcs_client.py` → Extract to storage backend
- `validibot/submissions/models.py` → Use storage backend for file uploads
- `config/settings/base.py` → Add `STORAGE_BACKEND` setting

#### 1.2 Task Queue Abstraction

Replace Cloud Tasks with Dramatiq:

```python
# validibot/core/tasks/base.py
class TaskBackend(ABC):
    @abstractmethod
    def enqueue(self, task_name: str, **kwargs) -> str:
        """Enqueue a task, return task ID."""
        pass

# validibot/core/tasks/dramatiq_backend.py
class DramatiqBackend(TaskBackend):
    """Dramatiq task queue for self-hosted deployments."""

# validibot/core/tasks/cloud_tasks_backend.py
class CloudTasksBackend(TaskBackend):
    """Cloud Tasks backend (existing code, refactored)."""
```

**Files to modify:**

- `validibot/core/tasks/cloud_tasks.py` → Refactor into backend class
- `validibot/validations/services/validation_run.py` → Use task backend interface
- New: `validibot/core/tasks/actors.py` → Dramatiq actor definitions

#### 1.3 Validator Execution Abstraction

Create a validator runner interface:

```python
# validibot/validations/services/runners/base.py
class ValidatorRunner(ABC):
    @abstractmethod
    def run(self, envelope: ValidationInputEnvelope) -> ValidationOutputEnvelope:
        """Execute validator and return result."""
        pass

# validibot/validations/services/runners/docker.py
class DockerValidatorRunner(ValidatorRunner):
    """Run validators in Docker containers (self-hosted)."""

    def run(self, envelope: ValidationInputEnvelope) -> ValidationOutputEnvelope:
        # Spawn container, wait for completion, return result
        container = self.client.containers.run(
            image=self._get_image(envelope.validator.type),
            environment={"INPUT_PATH": envelope_path},
            volumes={self.data_dir: {"bind": "/data", "mode": "rw"}},
            mem_limit="2g",
            remove=True,
        )
        return self._parse_output(envelope.run_id)

# validibot/validations/services/runners/cloud_run.py
class CloudRunValidatorRunner(ValidatorRunner):
    """Run validators as Cloud Run Jobs (existing code, refactored)."""
```

**Files to modify:**

- `validibot/validations/services/cloud_run/job_client.py` → Refactor into runner
- `validibot/validations/services/cloud_run/launcher.py` → Refactor into runner
- `validibot/validations/services/validation_callback.py` → Simplify (no callbacks in Docker mode)
- `validibot/validations/engines/energyplus.py` → Use runner interface
- `validibot/validations/engines/fmi.py` → Use runner interface

### Phase 2: Remove Callback Mechanism (Self-Hosted Mode)

In self-hosted mode with Docker, we don't need the callback mechanism:

```python
# Before (async with callback):
def execute_async_validator(step_run, envelope):
    # Launch job
    execution_name = run_validator_job(envelope)
    step_run.job_status = "RUNNING"
    step_run.save()
    # Return - callback will handle result
    return ValidationResult(passed=None)  # Pending

# After (sync wait):
def execute_async_validator(step_run, envelope):
    runner = get_validator_runner()  # DockerValidatorRunner
    output = runner.run(envelope)  # Blocks until complete
    return process_output(output)  # Immediate result
```

**Files to modify:**

- `validibot/validations/services/validation_run.py:execute_workflow_step()` → Handle sync execution
- `validibot/validations/services/validation_callback.py` → Keep for GCP mode, skip in Docker mode
- `validibot/validations/api/callbacks.py` → Keep for GCP mode

### Phase 3: Docker Compose Setup

Create production-ready Docker Compose configuration:

```yaml
# docker-compose.yml
version: "3.8"

services:
  # Reverse proxy - serves static/media files and proxies to Django
  caddy:
    image: caddy:2-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - validibot_media:/srv/media:ro # Public media (read-only)
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - web

  # Django web server - handles UI and API requests
  web:
    image: validibot/validibot:latest
    environment:
      - APP_ROLE=web
      - DATABASE_URL=postgres://validibot:${DB_PASSWORD}@db:5432/validibot
      - REDIS_URL=redis://redis:6379/0
      - STORAGE_BACKEND=local
      - TASK_BACKEND=dramatiq
      - VALIDATOR_RUNNER=docker
      - SECRET_KEY=${SECRET_KEY}
      - ALLOWED_HOSTS=${ALLOWED_HOSTS:-localhost}
    volumes:
      - validibot_data:/app/data # Private files (submissions, envelopes, outputs)
      - validibot_media:/app/media # Public media (avatars, blog images)
    depends_on:
      - db
      - redis

  # Background worker - processes validation tasks
  worker:
    image: validibot/validibot:latest
    environment:
      - APP_ROLE=worker
      - DATABASE_URL=postgres://validibot:${DB_PASSWORD}@db:5432/validibot
      - REDIS_URL=redis://redis:6379/0
      - STORAGE_BACKEND=local
      - TASK_BACKEND=dramatiq
      - VALIDATOR_RUNNER=docker
      - SECRET_KEY=${SECRET_KEY}
    volumes:
      - validibot_data:/app/data # Private files (shared with web)
      - validibot_media:/app/media # Public media (shared with web)
      - /var/run/docker.sock:/var/run/docker.sock # For spawning validator containers
    depends_on:
      - db
      - redis

  # PostgreSQL database
  db:
    image: postgres:16-alpine
    environment:
      - POSTGRES_USER=validibot
      - POSTGRES_PASSWORD=${DB_PASSWORD}
      - POSTGRES_DB=validibot
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U validibot"]
      interval: 10s
      timeout: 5s
      retries: 5

  # Redis - task queue broker and cache
  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  validibot_data: # Private files (submissions, validation outputs, envelopes)
  validibot_media: # Public media (avatars, blog images, workflow images)
  postgres_data: # Database storage
  redis_data: # Redis persistence
  caddy_data: # Caddy certificates
  caddy_config: # Caddy configuration
```

**Example Caddyfile:**

```
# Caddyfile
{$DOMAIN:localhost} {
    # Serve public media files directly
    handle /media/* {
        root * /srv
        file_server
    }

    # Proxy everything else to Django
    handle {
        reverse_proxy web:8000
    }
}
```

**New files:**

- `docker-compose.yml` - Production deployment
- `docker-compose.dev.yml` - Development overrides
- `docker-compose.podman.yml` - Podman-specific configuration
- `.env.example` - Environment variable template
- `Dockerfile` - Multi-stage build for web/worker

### Phase 4: Settings Restructure

Reorganize settings for multiple deployment modes:

```
config/settings/
├── base.py              # Shared settings
├── local.py             # Local development
├── production.py        # GCP production (existing, keep for now)
├── self_hosted.py       # Self-hosted production (NEW)
└── test.py              # Test settings
```

**Key settings for self-hosted:**

```python
# config/settings/self_hosted.py

# Storage
STORAGE_BACKEND = "local"  # or "s3" for AWS users
LOCAL_STORAGE_ROOT = env("STORAGE_ROOT", default="/app/data")

# Task queue
TASK_BACKEND = "dramatiq"
DRAMATIQ_BROKER = {
    "BROKER": "dramatiq.brokers.redis.RedisBroker",
    "OPTIONS": {"url": env("REDIS_URL")},
}

# Validator execution
VALIDATOR_RUNNER = "docker"
DOCKER_SOCKET = env("DOCKER_SOCKET", default="/var/run/docker.sock")

# Validator images (can be overridden)
VALIDATOR_IMAGES = {
    "energyplus": "validibot/validator-energyplus:latest",
    "fmi": "validibot/validator-fmi:latest",
}

# Feature flags (for edition gating)
EDITION = env("VALIDIBOT_EDITION", default="community")  # community, pro
```

### Phase 5: License/Edition Gating

Implement feature gating based on edition:

```python
# validibot/core/licensing.py

from enum import Enum
from django.conf import settings

class Edition(str, Enum):
    COMMUNITY = "community"
    PRO = "pro"

def get_edition() -> Edition:
    return Edition(settings.EDITION)

def is_feature_enabled(feature: str) -> bool:
    """Check if a feature is enabled for the current edition."""
    edition = get_edition()

    # Pro-only features
    FEATURE_MATRIX = {
        "advanced_validators": [Edition.PRO],
        "multiple_workspaces": [Edition.PRO],
        "team_management": [Edition.PRO],
    }

    allowed_editions = FEATURE_MATRIX.get(feature, [Edition.COMMUNITY, Edition.PRO])
    return edition in allowed_editions

# Usage in views:
from validibot.core.licensing import is_feature_enabled

class AdvancedValidatorViewSet(viewsets.ModelViewSet):
    def create(self, request, *args, **kwargs):
        if not is_feature_enabled("advanced_validators"):
            return Response(
                {"detail": "Advanced validators require Validibot Pro. Visit validibot.com/pricing"},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        return super().create(request, *args, **kwargs)
```

### Phase 6: Feature Removal & Repository Split

#### 6.1 Features to Remove from Open Source

Based on codebase analysis, the following features must be removed or simplified:

| Feature              | Current Location                                         | Action          | Reason                            |
| -------------------- | -------------------------------------------------------- | --------------- | --------------------------------- |
| **Billing/Stripe**   | `validibot/billing/`                                     | Remove entirely | SaaS-only, ~11 files + migrations |
| **Trial system**     | `billing/middleware.py`, `billing/context_processors.py` | Remove          | No trials in self-hosted          |
| **Plan selection**   | `templates/account/signup.html`                          | Simplify        | Single "self-hosted" tier         |
| **Pricing pages**    | `templates/marketing/pricing*.html`                      | Remove          | Not applicable                    |
| **Trial banner**     | `templates/billing/partial/trial_banner.html`            | Remove          | No trials                         |
| **Upgrade CTAs**     | Various templates                                        | Remove          | No upselling in OSS               |
| **Seat enforcement** | `billing/metering.py`                                    | Remove          | Unlimited users in OSS            |
| **Usage metering**   | `billing/metering.py`                                    | Remove          | No quotas in OSS                  |

#### 6.2 Features to Keep (Simplified)

| Feature                | Action           | Notes                                            |
| ---------------------- | ---------------- | ------------------------------------------------ |
| **Organization model** | Keep, simplify   | Auto-create single org on install                |
| **Membership/Roles**   | Keep             | Core multi-user functionality                    |
| **Tracking/Analytics** | Keep             | Internal analytics (local DB), useful for admins |
| **GCP backends**       | Keep as optional | Move to `[gcp]` extras, not default              |

#### 6.3 Billing Removal Checklist

```python
# Files to DELETE from open source repo:
validibot/billing/                    # Entire app (~11 files)
├── models.py                         # Plan, Subscription, CreditPurchase
├── views.py                          # 8 Stripe-related views
├── services.py                       # BillingService (Stripe API)
├── webhooks.py                       # Stripe webhook handlers
├── middleware.py                     # TrialExpiryMiddleware
├── context_processors.py             # billing_context()
├── metering.py                       # SeatEnforcer, BasicWorkflowMeter
├── plan_changes.py                   # PlanChangeService
├── admin.py
├── urls.py
└── migrations/                       # All billing migrations

# Templates to DELETE:
templates/billing/                    # All billing templates
templates/marketing/pricing*.html     # Pricing pages

# Settings to REMOVE:
STRIPE_PUBLIC_KEY
STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET
STRIPE_PRICE_IDS

# Middleware to REMOVE:
'validibot.billing.middleware.TrialExpiryMiddleware'

# Context processors to REMOVE:
'validibot.billing.context_processors.billing_context'

# URLs to REMOVE from urls_web.py:
path('billing/', include('validibot.billing.urls'))
```

#### 6.4 Organization Simplification

For open source, simplify to single-organization mode:

```python
# validibot/users/services.py (new or modified)

def get_or_create_default_organization():
    """
    In self-hosted mode, there's a single default organization.
    All users belong to this organization.
    """
    org, created = Organization.objects.get_or_create(
        slug="default",
        defaults={
            "name": "Default Organization",
            "is_personal": False,
        }
    )
    return org

def on_user_signup(user):
    """Simplified signup - add user to default org."""
    org = get_or_create_default_organization()
    Membership.objects.get_or_create(
        user=user,
        org=org,
        defaults={"is_active": True}
    )
    user.current_org = org
    user.save()
```

#### 6.5 Repository Structure

**Actual repository**: The commercial features live in `../validibot-commercial`.

```
validibot/                    # Public repo (AGPL) - github.com/validibot/validibot
├── validibot/
│   ├── core/                 # Core functionality
│   ├── users/                # Simplified user management
│   ├── workflows/            # Workflow engine
│   ├── validations/          # Validation engine
│   ├── submissions/          # File handling
│   ├── tracking/             # Internal analytics (keep)
│   └── api/                  # REST API
├── config/
│   └── settings/
│       ├── base.py
│       ├── local.py
│       └── self_hosted.py    # Default for Docker deployment
├── docker-compose.yml
├── Dockerfile
├── LICENSE                   # AGPL-3.0
└── README.md

validibot-commercial/         # Private repo (Commercial) - ../validibot-commercial
├── validibot_commercial/
│   ├── billing/              # Moved from main repo (Stripe, plans, subscriptions)
│   ├── multi_org/            # Multi-organization support
│   ├── advanced_validators/  # Advanced validator management (EnergyPlus, FMI, user-added)
│   ├── teams/                # Team management within orgs
│   └── __init__.py           # Auto-registers with Django
├── LICENSE                   # Commercial
├── setup.py
└── README.md

validibot-marketing/          # Separate repo for marketing website
├── src/                      # Static site source (Astro, Hugo, or similar)
├── public/
└── README.md
```

**Notes**:
- We use a single `validibot-commercial` repo for Pro features. Enterprise features (SSO, audit logs, advanced RBAC) will be added later when there's demand.
- The marketing site (`validibot-marketing`) is separate from the application. Since we're not hosting a SaaS, the self-hosted application doesn't need marketing pages, pricing tables, etc. The marketing site will be a static site hosted separately (e.g., on Netlify/Vercel) at validibot.com.

#### 6.6 Extension Points for Commercial Features

The open source version includes hooks where the commercial package can inject functionality:

```python
# validibot/core/extensions.py

from django.conf import settings
from importlib import import_module

class ExtensionRegistry:
    """Registry for Pro feature extensions."""

    _validators_hook = None
    _org_hook = None

    @classmethod
    def register_advanced_validators(cls, hook):
        """Commercial package registers advanced validator management here."""
        cls._validators_hook = hook

    @classmethod
    def get_advanced_validators(cls, org):
        """Returns advanced validators if commercial package is installed."""
        if cls._validators_hook:
            return cls._validators_hook(org)
        return []  # Empty in community edition

    @classmethod
    def register_multi_org(cls, hook):
        """Commercial package registers multi-org support here."""
        cls._org_hook = hook

    @classmethod
    def can_switch_org(cls, user):
        """Returns True if user can switch orgs (Pro feature)."""
        if cls._org_hook:
            return cls._org_hook.can_switch(user)
        return False  # Single org in community edition

# In validibot_commercial/__init__.py (Commercial package):
from validibot.core.extensions import ExtensionRegistry
from validibot_commercial.advanced_validators import AdvancedValidatorHook
from validibot_commercial.multi_org import MultiOrgHook

# Auto-register on import
ExtensionRegistry.register_advanced_validators(AdvancedValidatorHook())
ExtensionRegistry.register_multi_org(MultiOrgHook())
```

#### 6.7 Installation for Commercial Users

```bash
# Commercial users get access to private repo via GitHub
pip install git+https://github.com/validibot/validibot-commercial.git

# Or via private PyPI (if we set one up)
pip install validibot-commercial --index-url https://pypi.validibot.com/simple/
```

```python
# config/settings/self_hosted.py (Commercial installation)
INSTALLED_APPS = [
    # ... core apps ...
    'validibot_commercial',              # Auto-registers extensions
    'validibot_commercial.billing',      # Optional: if they want billing
    'validibot_commercial.multi_org',
    'validibot_commercial.advanced_validators',
]

EDITION = "pro"  # Enables Pro features
```

#### 6.8 Database Migration Strategy

The billing tables don't exist in the open source schema:

```python
# Open source migrations: No billing tables
# Commercial migrations: Add billing tables when commercial package is installed

# validibot_commercial/billing/migrations/0001_initial.py
# Creates Plan, Subscription, etc. tables

# This means:
# - Fresh OSS install: No billing tables
# - OSS → Commercial upgrade: Run `python manage.py migrate` to add billing tables
# - Commercial → OSS downgrade: Billing tables remain but are unused
```

## Migration Path

### For Existing GCP Deployment

The existing GCP deployment continues to work unchanged. The abstraction layers default to GCP backends when environment variables are set:

```python
# Auto-detection in settings
if env("GCP_PROJECT_ID", default=None):
    STORAGE_BACKEND = "gcs"
    TASK_BACKEND = "cloud_tasks"
    VALIDATOR_RUNNER = "cloud_run"
else:
    STORAGE_BACKEND = "local"
    TASK_BACKEND = "dramatiq"
    VALIDATOR_RUNNER = "docker"
```

### GCP Code Preservation

GCP-specific code is **not deleted**, just refactored into backend classes:

| Current Location                              | New Location                                |
| --------------------------------------------- | ------------------------------------------- |
| `core/tasks/cloud_tasks.py`                   | `core/tasks/backends/cloud_tasks.py`        |
| `validations/services/cloud_run/`             | `validations/services/runners/cloud_run.py` |
| `validations/services/validation_callback.py` | Kept, used only in GCP mode                 |

### Billing Code Migration

The billing system moves to the commercial package:

| Current Location                    | New Location                                            |
| ----------------------------------- | ------------------------------------------------------- |
| `validibot/billing/`                | `../validibot-commercial/validibot_commercial/billing/` |
| `templates/billing/`                | `../validibot-commercial/templates/billing/`            |
| `templates/marketing/pricing*.html` | Deleted (not needed for self-hosted)                    |

### Organization Simplification

| Current Behavior                    | Open Source Behavior               |
| ----------------------------------- | ---------------------------------- |
| Users can belong to multiple orgs   | Single default organization        |
| Org switching in UI                 | No org switching (hidden)          |
| Personal workspaces per user        | Single shared workspace            |
| Invitation system with seat limits  | Simplified invites, no seat limits |
| Trial subscriptions on org creation | No subscriptions/trials            |

## File Changes Summary

### New Files

| File                                      | Purpose                          |
| ----------------------------------------- | -------------------------------- |
| `docker-compose.yml`                      | Production Docker Compose        |
| `docker-compose.dev.yml`                  | Development overrides            |
| `Caddyfile`                               | Reverse proxy configuration      |
| `.env.example`                            | Environment template             |
| `Dockerfile`                              | Multi-stage build                |
| `config/settings/self_hosted.py`          | Self-hosted settings             |
| `core/storage/base.py`                    | Storage backend interface        |
| `core/storage/local.py`                   | Local filesystem storage         |
| `core/tasks/backends/base.py`             | Task backend interface           |
| `core/tasks/backends/dramatiq_backend.py` | Dramatiq implementation          |
| `core/tasks/actors.py`                    | Dramatiq actor definitions       |
| `validations/services/runners/base.py`    | Validator runner interface       |
| `validations/services/runners/docker.py`  | Docker container runner          |
| `core/licensing.py`                       | Edition/feature gating           |
| `core/extensions.py`                      | Extension registry for Pro hooks |
| `users/services.py`                       | Single-org helper functions      |
| `docs/self-hosted/`                       | Self-hosted documentation        |

### Deleted Files (from open source repo)

| File/Directory                      | Reason                        |
| ----------------------------------- | ----------------------------- |
| `validibot/billing/`                | Entire app moves to Pro       |
| `templates/billing/`                | Billing templates move to Pro |
| `templates/marketing/pricing*.html` | Not needed for self-hosted    |

### Modified Files

| File                                           | Changes                                 |
| ---------------------------------------------- | --------------------------------------- |
| `config/settings/base.py`                      | Add backend selection, remove billing   |
| `config/urls_web.py`                           | Remove billing URLs                     |
| `core/tasks/cloud_tasks.py`                    | Refactor into backend class             |
| `validations/services/cloud_run/gcs_client.py` | Extract to storage backend              |
| `validations/services/cloud_run/job_client.py` | Refactor into runner class              |
| `validations/services/cloud_run/launcher.py`   | Refactor into runner class              |
| `validations/services/validation_run.py`       | Use backend interfaces                  |
| `validations/services/validation_callback.py`  | Make GCP-only                           |
| `validations/engines/energyplus.py`            | Use runner interface                    |
| `validations/engines/fmi.py`                   | Use runner interface                    |
| `users/models.py`                              | Simplify org creation                   |
| `users/views.py`                               | Hide org switching UI                   |
| `templates/base.html`                          | Remove trial banner include             |
| `templates/account/signup.html`                | Remove plan selection                   |
| `pyproject.toml`                               | Add dramatiq, docker; make GCP optional |

### Dependencies to Add

```toml
# pyproject.toml
dependencies = [
    # ... existing ...
    "dramatiq[redis]>=1.15.0",
    "docker>=7.0.0",
]

[project.optional-dependencies]
gcp = [
    "google-cloud-tasks>=2.20.0",
    "google-cloud-run>=0.13.0",
    "google-cloud-storage>=2.14.0",
    "google-cloud-kms>=3.7.0",
]
```

## Testing Strategy

### Unit Tests

- Test each backend implementation independently
- Mock Docker/GCS/Cloud Tasks APIs
- Test feature gating logic

### Integration Tests

- Docker Compose stack tests (CI with docker-compose)
- GCP backend tests (existing, keep running)
- End-to-end validation run tests for both modes

### CI/CD

```yaml
# .github/workflows/test.yml
jobs:
  test-self-hosted:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Start services
        run: docker compose -f docker-compose.test.yml up -d
      - name: Run tests
        run: docker compose exec web pytest tests/

  test-gcp:
    runs-on: ubuntu-latest
    # Existing GCP integration tests
```

## Documentation Plan

### Self-Hosted Documentation (`docs/self-hosted/`)

1. **Quick Start** - Get running in 5 minutes with Docker Compose
2. **Installation Guide** - Detailed setup instructions
3. **Configuration Reference** - All environment variables
4. **Security Guide** - Docker socket, Podman, network isolation
5. **Upgrading** - How to upgrade between versions
6. **Backup & Restore** - Data backup procedures
7. **Troubleshooting** - Common issues and solutions

### Pro/Enterprise Documentation (Private)

1. **License Installation** - How to activate Pro/Enterprise
2. **Custom Validators** - Creating and deploying custom validators
3. **Team Management** - Setting up teams and permissions
4. **SSO Configuration** - SAML/OIDC setup (Enterprise)
5. **Audit Logs** - Accessing and exporting audit data (Enterprise)

## Timeline Estimate

| Phase         | Description               | Relative Effort |
| ------------- | ------------------------- | --------------- |
| Phase 1       | Abstract GCP dependencies | Large           |
| Phase 2       | Remove callback mechanism | Medium          |
| Phase 3       | Docker Compose setup      | Medium          |
| Phase 4       | Settings restructure      | Small           |
| Phase 5       | License/edition gating    | Medium          |
| Phase 6       | Repository split          | Small           |
| Documentation | Self-hosted docs          | Medium          |
| Testing       | Integration tests         | Medium          |

## Risks and Mitigations

| Risk                                 | Mitigation                                                |
| ------------------------------------ | --------------------------------------------------------- |
| Docker socket security               | Document risks, recommend Podman, enforce resource limits |
| Breaking existing GCP deployment     | Abstraction layers default to GCP when env vars present   |
| Complexity of supporting two modes   | Clean backend interfaces, comprehensive tests             |
| Support burden for self-hosted users | Good documentation, community forum, Pro tier for support |
| Custom validators as attack vector   | Image allowlist, resource limits, documentation           |

## Success Criteria

1. **Self-hosted deployment works** - User can `docker compose up` and have working Validibot
2. **GCP deployment still works** - No regression in existing functionality
3. **Feature gating works** - Pro features properly restricted in Community edition
4. **Documentation complete** - Users can self-serve installation and configuration
5. **Security documented** - Clear guidance on Docker socket risks and mitigations

## References

### Similar Projects Studied

- [Sidekiq](https://sidekiq.org/) - Open-core Ruby background jobs (pricing model)
- [GitLab](https://about.gitlab.com/pricing/feature-comparison/) - CE/EE feature comparison
- [n8n](https://docs.n8n.io/hosting/community-edition-features/) - Self-hosted workflow automation
- [Sentry](https://develop.sentry.dev/self-hosted/) - Self-hosted error tracking
- [PostHog](https://posthog.com/docs/self-host) - Self-hosted analytics

### Container Orchestration Research

- [Drone CI architecture](https://docs.drone.io/) - Docker socket pattern
- [GitLab Runner security](https://docs.gitlab.com/runner/security/) - gVisor, Kata containers
- [Buildkite agent security](https://buildkite.com/docs/agent/v3/securing) - Rootless mode, isolation

### Task Queue Comparison

- [Dramatiq documentation](https://dramatiq.io/)
- [Dramatiq vs Celery](https://dramatiq.io/motivation.html)

## Appendix A: Current GCP Architecture Reference

```
┌─────────────────────────────────────────────────────────────────┐
│                     Current GCP Architecture                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐    │
│  │ Cloud Run    │     │ Cloud Run    │     │ Cloud SQL    │    │
│  │ Service      │────►│ Service      │────►│ PostgreSQL   │    │
│  │ (Web)        │     │ (Worker)     │     │              │    │
│  └──────────────┘     └──────────────┘     └──────────────┘    │
│         │                    ▲                                   │
│         │                    │ Callback                          │
│         ▼                    │                                   │
│  ┌──────────────┐     ┌──────────────┐                          │
│  │ Cloud Tasks  │────►│ Cloud Run    │                          │
│  │ Queue        │     │ Jobs         │                          │
│  └──────────────┘     │ (Validators) │                          │
│                       └──────────────┘                          │
│                              │                                   │
│                              ▼                                   │
│                       ┌──────────────┐                          │
│                       │ Cloud Storage│                          │
│                       │ (GCS)        │                          │
│                       └──────────────┘                          │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

Key files:

- `validibot/core/tasks/cloud_tasks.py` - Cloud Tasks integration
- `validibot/validations/services/cloud_run/job_client.py` - Cloud Run Jobs API
- `validibot/validations/services/cloud_run/launcher.py` - Job launcher
- `validibot/validations/services/cloud_run/gcs_client.py` - GCS client
- `validibot/validations/services/validation_callback.py` - Callback handler
- `config/settings/production.py` - GCP settings

## Appendix B: Self-Hosted Architecture Target

```
┌─────────────────────────────────────────────────────────────────┐
│                   Self-Hosted Architecture                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐    │
│  │ Web          │     │ Worker       │     │ PostgreSQL   │    │
│  │ Container    │────►│ Container    │────►│ Container    │    │
│  │ (Django)     │     │ (Dramatiq)   │     │              │    │
│  └──────────────┘     └──────────────┘     └──────────────┘    │
│                              │                                   │
│                              │ Docker Socket                     │
│                              ▼                                   │
│                       ┌──────────────┐                          │
│                       │ Validator    │                          │
│                       │ Containers   │                          │
│                       │ (Spawned)    │                          │
│                       └──────────────┘                          │
│                              │                                   │
│  ┌──────────────┐           │                                   │
│  │ Redis        │◄──────────┘                                   │
│  │ Container    │                                               │
│  └──────────────┘     ┌──────────────┐                          │
│                       │ Shared       │                          │
│                       │ Volume       │                          │
│                       │ (Data)       │                          │
│                       └──────────────┘                          │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

Key differences:

- No Cloud Tasks → Dramatiq with Redis
- No Cloud Run Jobs → Docker containers spawned via socket
- No GCS → Local filesystem (shared volume)
- No callbacks → Synchronous wait pattern
- Single Docker Compose stack → Simple deployment
