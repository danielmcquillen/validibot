# Execution Backends

Validibot supports multiple deployment targets through an abstracted execution backend system. This document describes how the platform orchestrates advanced validator containers across different infrastructure.

For the container interface that validators must implement, see [Advanced Validator Container Interface](validator_architecture.md).

## Overview

The execution layer sits between the validator and the infrastructure:

```
Validator → ExecutionBackend → Infrastructure
                          ↓
          ┌───────────────┼───────────────┐
          ↓               ↓               ↓
  DockerComposeBackend   GCPBackend      AWSBackend
  (Docker socket)   (Cloud Run+GCS)    (future)
```

Each backend handles:

- Preparing input data (uploading envelopes to storage)
- Launching validator containers
- Collecting results (synchronously or via callbacks)
- Cleaning up resources

## Backend Selection

The backend is selected via the `VALIDATOR_RUNNER` setting:

| Setting Value        | Backend                         | Execution Model |
| -------------------- | ------------------------------- | --------------- |
| `"docker"`           | `DockerComposeExecutionBackend` | Synchronous     |
| `"google_cloud_run"` | `GCPExecutionBackend`           | Asynchronous    |

If `VALIDATOR_RUNNER` is not set, the system auto-detects:

- If `GCP_PROJECT_ID` is set → Uses GCP backend
- Otherwise → Uses Docker backend (Docker Compose)

## Execution Models

### Synchronous (Docker Compose)

Used for Docker Compose deployments where validators run as local Docker containers.

```
1. Validator calls backend.execute(request)
2. Backend writes input envelope to local storage (file:// URI)
3. Backend spawns Docker container and waits for completion
4. Backend reads output envelope from local storage
5. Returns complete ExecutionResponse with results
```

**Characteristics:**

- Blocking call — validation completes before returning
- Simple deployment — just Docker and shared volumes
- Resource limits enforced via Docker
- Container cleanup handled by labels (Ryuk pattern)

### Asynchronous (GCP Cloud Run)

Used for GCP deployments where validators run as Cloud Run Jobs.

```
1. Validator calls backend.execute(request)
2. Backend uploads input envelope to GCS (gs:// URI)
3. Backend triggers Cloud Run Job (non-blocking)
4. Returns ExecutionResponse with is_complete=False
5. Container POSTs callback to Django when complete
6. Callback handler loads output envelope from GCS
```

**Characteristics:**

- Non-blocking — validation runs in background
- Scalable — Cloud Run handles concurrency
- Callback-based — results arrive via authenticated HTTP POST
- IAM-secured — no shared secrets, Google-signed ID tokens

## Two-Layer Architecture

The execution system uses a two-layer architecture:

```
ExecutionBackend (high-level orchestration)
    ├── Storage management (upload/download envelopes)
    ├── Envelope building (input envelope construction)
    ├── Status checking (check_status() for reconciliation)
    └── Delegates to → ValidatorRunner (low-level container execution)
                            ├── Container spawn/wait/remove
                            ├── Security hardening (cap_drop, read_only, etc.)
                            ├── Container labeling (Ryuk pattern)
                            └── Container cleanup (orphan sweep, startup cleanup)
```

**Why two layers?**

- **ExecutionBackend** handles orchestration: it knows about storage URIs, envelopes, and the callback protocol. It doesn't know how containers are spawned.
- **ValidatorRunner** handles container lifecycle: it knows about Docker APIs and Cloud Run Jobs. It doesn't know about envelopes or callbacks.

This separation means new deployment targets only need a new runner (for container execution) and a new backend (for storage integration), without duplicating orchestration logic.

| Layer | Docker Compose | GCP |
|-------|---------------|-----|
| Backend | `DockerComposeExecutionBackend` | `GCPExecutionBackend` |
| Runner | `DockerValidatorRunner` | `GoogleCloudRunValidatorRunner` |
| Storage | Local filesystem (`file://`) | GCS (`gs://`) |
| Execution | Sync (blocking) | Async (callback) |

## Code Location

```
validibot/validations/services/
├── execution/                    # Backend layer (high-level)
│   ├── __init__.py               # Exports get_execution_backend()
│   ├── base.py                   # ExecutionBackend ABC, ExecutionRequest, ExecutionResponse
│   ├── docker_compose.py         # DockerComposeExecutionBackend
│   ├── gcp.py                    # GCPExecutionBackend
│   └── registry.py               # Backend selection and caching
├── runners/                      # Runner layer (low-level)
│   ├── __init__.py               # Exports get_validator_runner()
│   ├── base.py                   # ValidatorRunner ABC, ExecutionStatus, ExecutionResult
│   ├── docker.py                 # DockerValidatorRunner (labels, security, cleanup)
│   └── google_cloud_run.py       # GoogleCloudRunValidatorRunner
└── validation_callback.py        # Callback processing (for async backends)
```

## Usage in Validators

```python
from validibot.validations.services.execution import get_execution_backend
from validibot.validations.services.execution.base import ExecutionRequest

backend = get_execution_backend()

request = ExecutionRequest(
    run=validation_run,
    validator=validator,
    submission=submission,
    step=workflow_step,
)

response = backend.execute(request)

if backend.is_async:
    # Results will arrive via callback
    return ValidationResult(passed=None, issues=[], stats={...})
else:
    # Results available immediately
    return process_output_envelope(response.output_envelope)
```

## Docker Compose Backend Details

### Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Docker Host                                                     │
│                                                                  │
│  ┌──────────────────┐    ┌─────────────────────────────────┐    │
│  │  Django + Worker │    │  Validator Container            │    │
│  │                  │    │  ($GCP_APP_NAME-validator-X)    │    │
│  │  - Web app       │───▶│                                 │    │
│  │  - Celery        │    │  Reads: file:///input           │    │
│  │                  │◀───│  Writes: file:///output         │    │
│  └──────────────────┘    └─────────────────────────────────┘    │
│           │                         │                            │
│           ▼                         ▼                            │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Shared Storage Volume                       │   │
│  │              /app/storage (Docker volume)                │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

### Configuration

```python
# config/settings/production.py (when DEPLOYMENT_TARGET=docker_compose)
VALIDATOR_RUNNER = "docker"
VALIDATOR_RUNNER_OPTIONS = {
    "memory_limit": "4g",
    "cpu_limit": "2.0",
    "network": None,            # None = no network (default, most secure)
    "timeout_seconds": 3600,
}

# Container images
VALIDATOR_IMAGE_TAG = "latest"
VALIDATOR_IMAGE_REGISTRY = ""  # Or your private registry
```

### Network Isolation (Security)

By default, advanced validator containers run with **no network access** (`network_mode='none'`). This is the most secure configuration because:

- Containers cannot reach other services (web, database, redis)
- Containers cannot access the internet
- All I/O happens via the shared storage volume

This works because:

1. Input files are written to the shared volume before the container starts
2. The container reads inputs and writes outputs to the same volume
3. The worker reads the output after the container exits

**When to enable network access:**

Set `VALIDATOR_NETWORK` only if advanced validators need to:

- Download files from external URLs during execution
- Call external APIs as part of validation logic

```yaml
# In docker-compose.*.yml, uncomment to enable network:
environment:
  - VALIDATOR_NETWORK=validibot_validibot
```

With network enabled, advanced validator containers can reach:

- Other containers on the same Docker network
- External internet (if the host has connectivity)

### Compose Project Naming Requirements

The Docker Compose backend requires specific naming for networks and volumes. By default, Docker Compose prefixes resource names with the project name (derived from the directory name or `COMPOSE_PROJECT_NAME`).

The shipped compose files assume `COMPOSE_PROJECT_NAME=validibot`, which creates:

| Resource       | Full Name                                   |
| -------------- | ------------------------------------------- |
| Network        | `validibot_validibot`                       |
| Storage Volume | `validibot_validibot_storage` (production)  |
| Storage Volume | `validibot_validibot_local_storage` (local) |

These names are configured in the compose files via environment variables:

```yaml
environment:
  - VALIDATOR_NETWORK=validibot_validibot
  - VALIDATOR_STORAGE_VOLUME=validibot_validibot_storage
```

**If you change the project name** (via `COMPOSE_PROJECT_NAME` or running from a different directory), you must update these environment variables to match. Otherwise, the worker cannot attach advanced validator containers to the correct network or volume.

To check your current project name:

```bash
# The project name is the prefix before the underscore in container names
docker compose -f docker-compose.production.yml ps --format "{{.Name}}"
# Example output: validibot_web_1 → project name is "validibot"
```

To override explicitly:

```bash
# Set project name explicitly
COMPOSE_PROJECT_NAME=validibot docker compose -f docker-compose.production.yml up -d
```

### Private Registry Authentication

By default, validator images are pulled from Docker Hub. If you're using a private registry (GitHub Container Registry, AWS ECR, Google Artifact Registry, etc.), you need to configure Docker credentials on the host.

**Option 1: Docker login on the host**

```bash
# Log in to your registry on the Docker host
docker login ghcr.io -u USERNAME -p TOKEN

# Or for AWS ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 123456789.dkr.ecr.us-east-1.amazonaws.com
```

The Docker daemon stores credentials in `~/.docker/config.json` and uses them for pulls. Since the worker spawns containers via the host's Docker socket, these credentials apply to validator image pulls automatically.

**Option 2: Pass credentials via environment**

For registries that support credential helpers, configure them on the host:

```bash
# Install and configure credential helper (e.g., for GCR)
gcloud auth configure-docker
```

**Image naming:**

Configure the validator image registry in your environment:

```bash
# .envs/.production/.docker-compose/.django
VALIDATOR_IMAGE_REGISTRY=ghcr.io/your-org
VALIDATOR_IMAGE_TAG=v1.2.0
```

Images are pulled as `{VALIDATOR_IMAGE_REGISTRY}/$GCP_APP_NAME-validator-{type}:{tag}`. For example:

- `ghcr.io/your-org/$GCP_APP_NAME-validator-energyplus:v1.2.0`
- `ghcr.io/your-org/$GCP_APP_NAME-validator-fmu:v1.2.0`

**Image availability:**

The Docker backend does not automatically pull images. Ensure validator images are available before running validations:

```bash
# Pre-pull images on the host
docker pull ghcr.io/your-org/$GCP_APP_NAME-validator-energyplus:v1.2.0
```

Or configure a pull policy by extending the runner options if automatic pulls are needed.

### Container Management

Validator containers are labeled for identification and cleanup:

```
org.validibot.managed=true
org.validibot.run_id=<run-id>
org.validibot.validator=<slug>
org.validibot.started_at=<iso>
org.validibot.timeout_seconds=N
```

Cleanup strategies:

1. **On-demand** — Container removed after run completes
2. **Periodic sweep** — Background task every 10 minutes
3. **Startup cleanup** — Worker removes leftover containers on start

Management command:

```bash
# Show what would be cleaned up
python manage.py cleanup_containers --dry-run

# Remove orphaned containers
python manage.py cleanup_containers

# Remove ALL managed containers
python manage.py cleanup_containers --all
```

## GCP Backend Details

For detailed GCP architecture including Cloud Run Jobs, IAM configuration, and callback flow, see:

- [Validator Containers (Cloud Run)](../validator_jobs_cloud_run.md) — Job execution and callbacks
- [GCP Deployment](../google_cloud/deployment.md) — Service deployment
- [IAM & Service Accounts](../google_cloud/iam.md) — Security configuration

### Key Concepts

**Web/Worker Split:**

- `$GCP_APP_NAME-web` — Public UI and API
- `$GCP_APP_NAME-worker` — Private, receives callbacks from validator jobs

**Callback Authentication:**

- Validator jobs use Google-signed ID tokens
- Worker service requires IAM authentication
- No shared secrets in envelopes

**Storage:**

- Input/output envelopes stored in GCS
- URIs use `gs://` scheme
- Service accounts need appropriate storage permissions

## Status Checking

The `ExecutionBackend` base class provides a `check_status()` method for querying the state of a running or completed execution:

```python
def check_status(self, execution_id: str) -> ExecutionResponse | None:
    """Check execution status. Returns None if not supported."""
    return None
```

| Backend | Behavior |
|---------|----------|
| `DockerComposeExecutionBackend` | Queries Docker daemon for container state. Primarily for debugging (sync execution already returns results). |
| `GCPExecutionBackend` | Queries Cloud Run Jobs API for execution state. Used by reconciliation to recover lost callbacks. |

This method is **not abstract** — backends that don't need status checking (sync backends) can leave the default `None` return.

## Container Cleanup

Container lifecycle management happens at the **runner layer**, not the backend layer:

### Docker Compose (three strategies)

1. **Immediate cleanup** — `container.remove(force=True)` in the runner's `finally` block after every execution
2. **Periodic sweep** — `cleanup_orphaned_containers()` runs via Celery Beat every 10 minutes, removes containers past timeout + grace period
3. **Startup cleanup** — `cleanup_all_managed_containers()` runs in `AppConfig.ready()`, removes all labeled containers from previous worker incarnation

All strategies use Docker container labels (`org.validibot.managed`, `org.validibot.run_id`, etc.) for identification.

### GCP Cloud Run

Cloud Run Jobs are ephemeral — there's nothing to clean up at the container level. Error recovery is handled by the reconciliation system (see below).

## Error Recovery

### Lost Callback Recovery (GCP)

If a Cloud Run Job completes but its callback never reaches Django (network failure, container crash before POST), the `cleanup_stuck_runs` management command attempts reconciliation:

1. Finds runs stuck in `RUNNING` status past the timeout threshold
2. For GCP runs, checks `step_run.output` for `execution_name` metadata
3. Queries Cloud Run Jobs API via `GCPExecutionBackend.check_status()`
4. Based on result:
   - **Still running**: Skips the run (legitimately in progress)
   - **Succeeded**: Constructs a synthetic callback and processes through `ValidationCallbackService` (reuses existing idempotency, finding persistence, assertion evaluation)
   - **Failed**: Marks the run as `FAILED` with the Cloud Run error message
   - **API error**: Falls through to simple `TIMED_OUT` marking

This reconciliation runs automatically when `cleanup_stuck_runs` is scheduled (typically every 10 minutes via Cloud Scheduler).

### Stuck Run Timeout (All Backends)

For runs where reconciliation is not possible (non-GCP, no execution metadata, API errors), the command marks them as `TIMED_OUT` after the configured threshold (default: 30 minutes).

```bash
# Manual invocation
python manage.py cleanup_stuck_runs
python manage.py cleanup_stuck_runs --timeout-minutes 60
python manage.py cleanup_stuck_runs --dry-run
```

## Adding a New Backend

To support a new deployment target (e.g., AWS):

1. Create `validibot/validations/services/execution/aws.py`
2. Implement `ExecutionBackend` abstract class
3. Register in `registry.py`
4. Add setting value to backend selection logic

```python
# aws.py
from .base import ExecutionBackend, ExecutionRequest, ExecutionResponse

class AWSExecutionBackend(ExecutionBackend):
    is_async = True  # or False for synchronous

    def execute(self, request: ExecutionRequest) -> ExecutionResponse:
        # Upload envelope to S3
        # Trigger ECS task or Lambda
        # Return response
        ...
```
