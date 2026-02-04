# Execution Backends

Validibot supports multiple deployment targets through an abstracted execution backend system. This document describes how the platform orchestrates advanced validator containers across different infrastructure.

For the container interface that validators must implement, see [Advanced Validator Container Interface](validator_architecture.md).

## Overview

The execution layer sits between the validation engine and the infrastructure:

```
Validation Engine → ExecutionBackend → Infrastructure
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

| Setting Value | Backend | Execution Model |
|---------------|---------|-----------------|
| `"docker"` | `DockerComposeExecutionBackend` | Synchronous |
| `"google_cloud_run"` | `GCPExecutionBackend` | Asynchronous |

If `VALIDATOR_RUNNER` is not set, the system auto-detects:

- If `GCP_PROJECT_ID` is set → Uses GCP backend
- Otherwise → Uses Docker backend (Docker Compose)

## Execution Models

### Synchronous (Docker Compose)

Used for Docker Compose deployments where validators run as local Docker containers.

```
1. Engine calls backend.execute(request)
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
1. Engine calls backend.execute(request)
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

## Code Location

```
validibot/validations/services/execution/
├── __init__.py          # Exports get_execution_backend()
├── base.py              # ExecutionBackend ABC, ExecutionRequest, ExecutionResponse
├── docker_compose.py    # DockerComposeExecutionBackend (Docker)
├── gcp.py               # GCPExecutionBackend (Cloud Run)
└── registry.py          # Backend selection and caching
```

## Usage in Validation Engines

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
┌─────────────────────────────────────────────────────────┐
│  Docker Host                                            │
│                                                         │
│  ┌──────────────────┐    ┌──────────────────────────┐  │
│  │  Django + Worker │    │  Validator Container     │  │
│  │                  │    │  (validibot-validator-X) │  │
│  │  - Web app       │───▶│                          │  │
│  │  - Celery        │    │  Reads: file:///input    │  │
│  │                  │◀───│  Writes: file:///output  │  │
│  └──────────────────┘    └──────────────────────────┘  │
│           │                         │                   │
│           ▼                         ▼                   │
│  ┌─────────────────────────────────────────────────┐   │
│  │              Shared Storage Volume               │   │
│  │              /app/storage (Docker volume)        │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### Configuration

```python
# config/settings/production.py (when DEPLOYMENT_TARGET=docker_compose)
VALIDATOR_RUNNER = "docker"
VALIDATOR_RUNNER_OPTIONS = {
    "memory_limit": "4g",
    "cpu_limit": "2.0",
    "network": "validibot_validibot",  # Docker network name
    "timeout_seconds": 3600,
}

# Container images
VALIDATOR_IMAGE_TAG = "latest"
VALIDATOR_IMAGE_REGISTRY = ""  # Or your private registry
```

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

- `validibot-web` — Public UI and API
- `validibot-worker` — Private, receives callbacks from validator jobs

**Callback Authentication:**

- Validator jobs use Google-signed ID tokens
- Worker service requires IAM authentication
- No shared secrets in envelopes

**Storage:**

- Input/output envelopes stored in GCS
- URIs use `gs://` scheme
- Service accounts need appropriate storage permissions

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
