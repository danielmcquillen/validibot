# Validator Architecture

This document describes the architecture for running validations. Validibot supports
multiple deployment targets through an abstracted execution backend system.

## Execution Backend Architecture

Validibot uses an `ExecutionBackend` abstraction to support different deployment targets.
The execution layer sits between the validation engine and the infrastructure:

```
Engine (energyplus.py) → ExecutionBackend → Infrastructure
                              ↓
              ┌───────────────┼───────────────┐
              ↓               ↓               ↓
    SelfHostedBackend    GCPBackend      AWSBackend
    (Docker socket)    (Cloud Run+GCS)   (future)
```

### Backend Selection

The backend is selected via the `VALIDATOR_RUNNER` setting:

- `"docker"` → SelfHostedExecutionBackend (synchronous, local Docker)
- `"google_cloud_run"` → GCPExecutionBackend (async, Cloud Run Jobs)

If `VALIDATOR_RUNNER` is not set, the system auto-detects:

- If `GCP_PROJECT_ID` is set → Uses GCP backend
- Otherwise → Uses Docker backend (self-hosted)

### Execution Models

**Synchronous (self-hosted Docker):**

1. Engine calls `backend.execute(request)`
2. Backend uploads input envelope to local storage (`file://` URI)
3. Backend runs Docker container and waits for completion
4. Backend reads output envelope from local storage
5. Returns complete `ExecutionResponse` with results

**Asynchronous (GCP Cloud Run):**

1. Engine calls `backend.execute(request)`
2. Backend uploads input envelope to GCS (`gs://` URI)
3. Backend triggers Cloud Run Job (non-blocking)
4. Returns `ExecutionResponse` with `is_complete=False`
5. Container POSTs callback to Django when complete
6. Callback handler loads output envelope from GCS

### Code Location

```
validibot/validations/services/execution/
├── __init__.py          # Exports get_execution_backend()
├── base.py              # ExecutionBackend ABC, ExecutionRequest, ExecutionResponse
├── self_hosted.py       # SelfHostedExecutionBackend (Docker)
├── gcp.py               # GCPExecutionBackend (Cloud Run)
└── registry.py          # Backend selection and caching
```

### Usage in Engines

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

---

## GCP Deployment Architecture

The following sections describe the GCP-specific architecture using Cloud Run Services
and Cloud Run Jobs.

## Validator Assets

Validator assets (weather files, reference data, etc.) are stored in GCS under:

```
gs://<bucket>/validator_assets/<asset_type>/
```

Each environment has its own bucket:
- **dev**: `validibot-files-dev`
- **staging**: `validibot-files-staging`
- **prod**: `validibot-files`

### Weather file catalog (EnergyPlus)

- Store all EPW files under: `gs://<bucket>/validator_assets/weather_data/<file>.epw`
- Use `just sync-weather <env>` to sync local weather files from `../weather_data/` to GCS
- Use `just list-weather <env>` to list weather files in a bucket
- Grant validator service accounts only `roles/storage.objectViewer` on the bucket so runtime code can read but not modify.
- Keep the catalog in the same region as the jobs to avoid cross-region egress.
- Optionally enable object versioning on the bucket for accidental delete/overwrite recovery; the job should still treat the catalog as immutable.
- Tests that need a weather file should verify it exists at the prefix (and may upload a fixture in non-prod) but production flows must only read from the catalog.

## Repository Structure

The validator system spans three codebases:

- **This repo (`validibot/`)**: the Django web + worker services (Cloud Run Services).
- **`validibot_shared`**: shared Pydantic envelope models (installed here; see `validibot_shared_dev/` for the local checkout).
- **`validibot_validators`**: validator job containers (see `validibot_validators_dev/` for the local checkout).

```
validibot/ (this repo)
├── validibot/                               # Django app (Cloud Run Services)
│   └── validations/
│       ├── services/cloud_run/              # Launcher + Jobs API client
│       └── api/callbacks.py                 # Worker-only callback endpoint
│
├── validibot_shared_dev/                           # Local checkout of ../validibot_shared (schemas + envelopes)
│   └── validibot_shared/validations/envelopes.py   # ExecutionContext, ValidationCallback, etc.
│
└── validibot_validators_dev/                       # Local checkout of ../validibot_validators (Cloud Run Job containers)
    └── validators/
        ├── core/callback_client.py          # Posts callbacks (ID token)
        ├── energyplus/                      # EnergyPlus validator container
        └── fmi/                             # FMI validator container
```

## Data Flow

### 1. User Submits Model

```
User → Django REST API
  POST /api/v1/orgs/{org_slug}/workflows/{workflow_identifier}/runs/
  Body: IDF or epJSON content
```

Note: The `workflow_identifier` can be either the workflow's slug (preferred) or its numeric database ID.

### 2. Django Creates Validation Job

```python
# In Django (Cloud Run Service)
from validibot_shared.energyplus import EnergyPlusInputEnvelope, EnergyPlusInputs
from validibot_shared.validations.envelopes import InputFileItem, ExecutionContext

# Upload user's model to GCS
model_uri = upload_to_gcs(submission.content, "model.idf")
weather_uri = "gs://bucket/weather/USA_CA_SF.epw"

# Create typed input envelope
input_envelope = EnergyPlusInputEnvelope(
    run_id=str(run.id),
    validator=ValidatorInfo(
        id=str(validator.id),
        type="energyplus",
        version="24.2.0"
    ),
    org=OrganizationInfo(
        id=str(org.id),
        name=org.name
    ),
    workflow=WorkflowInfo(
        id=str(workflow.id),
        step_id=str(step.id),
        step_name=step.name
    ),
    input_files=[
        InputFileItem(
            name="model.idf",
            mime_type="application/vnd.energyplus.idf",
            role="primary-model",
            uri=model_uri
        ),
        InputFileItem(
            name="weather.epw",
            mime_type="application/vnd.energyplus.epw",
            role="weather",
            uri=weather_uri
        ),
    ],
    inputs=EnergyPlusInputs(
        timestep_per_hour=4,
        output_variables=["Zone Mean Air Temperature"],
        invocation_mode="cli"
    ),
    context=ExecutionContext(
        # This should target the worker service base URL (WORKER_URL),
        # not the public domain (SITE_URL).
        callback_url="https://validibot-worker.example.a.run.app/api/v1/validation-callbacks/",
        callback_id="cb-uuid-here",
        execution_bundle_uri=f"gs://bucket/{org.id}/{run.id}/",
        timeout_seconds=3600,
    )
)

# Upload input envelope to GCS
input_uri = f"gs://bucket/{org.id}/{run.id}/input.json"
upload_envelope(input_envelope, input_uri)

# Trigger Cloud Run Job via Jobs API (non-blocking)
from django.conf import settings

from validibot.validations.services.cloud_run.job_client import run_validator_job

execution_name = run_validator_job(
    project_id=settings.GCP_PROJECT_ID,
    region=settings.GCP_REGION,
    job_name="validibot-validator-energyplus",
    input_uri=input_uri,
)
```

### 3. Cloud Run Job Executes

```python
# In validators/energyplus/main.py
from validibot_shared.energyplus.envelopes import EnergyPlusInputEnvelope, EnergyPlusOutputEnvelope
from validators.shared.envelope_loader import load_input_envelope
from validators.shared.gcs_client import upload_envelope
from validators.shared.callback_client import post_callback

# Load input envelope
input_envelope = load_input_envelope(EnergyPlusInputEnvelope)

# Download input files from GCS
for file_item in input_envelope.input_files:
    download_file(file_item.uri, f"/tmp/{file_item.name}")

# Run EnergyPlus
result = subprocess.run([
    "energyplus",
    "--weather", "/tmp/weather.epw",
    "/tmp/model.idf"
])

# Extract metrics from SQL database
metrics = extract_metrics("/tmp/eplusout.sql")

# Create typed output envelope
output_envelope = EnergyPlusOutputEnvelope(
    run_id=input_envelope.run_id,
    validator=input_envelope.validator,
    status=ValidationStatus.SUCCESS,
    timing={
        "started_at": started_at,
        "finished_at": finished_at
    },
    messages=[
        ValidationMessage(
            severity=Severity.INFO,
            text="Simulation completed successfully"
        )
    ],
    metrics=[
        ValidationMetric(
            name="electricity_kwh",
            value=metrics.electricity_kwh,
            unit="kWh"
        )
    ],
    outputs=EnergyPlusOutputs(
        outputs=EnergyPlusSimulationOutputs(
            eplusout_sql=Path("/tmp/eplusout.sql")
        ),
        metrics=metrics,
        logs=logs,
        energyplus_returncode=0,
        execution_seconds=duration,
        invocation_mode="cli"
    )
)

# Upload output envelope to GCS
output_uri = f"{input_envelope.context.execution_bundle_uri}output.json"
upload_envelope(output_envelope, output_uri)

# POST callback to Django
post_callback(
    callback_url=input_envelope.context.callback_url,
    run_id=input_envelope.run_id,
    status=ValidationStatus.SUCCESS,
    result_uri=output_uri
)
```

### 4. Django Receives Callback

```python
# In Django callback endpoint
@api_view(['POST'])
def validation_callback(request):
    # IAM authentication is handled by Cloud Run (ID token)
    callback = ValidationCallback.model_validate(request.data)

    # Download full output envelope from GCS
    output_envelope = download_envelope(
        callback.result_uri,
        EnergyPlusOutputEnvelope  # Type determined by validator.type
    )

    # Update database with results
    run = ValidationRun.objects.get(id=callback.run_id)
    run.status = callback.status
    run.metrics = output_envelope.metrics
    run.messages = output_envelope.messages
    run.save()

    # Trigger next workflow step if needed
    if run.workflow_step.next_step:
        trigger_next_step(run.workflow)

    return Response({"status": "received"})
```

## Type Safety Flow

### Django Side (Creating Jobs)

```python
# Django knows which envelope type to use based on validator.type
if validator.type == "energyplus":
    from validibot_shared.energyplus import EnergyPlusInputEnvelope, EnergyPlusInputs
    envelope = EnergyPlusInputEnvelope(
        inputs=EnergyPlusInputs(...)  # Fully typed!
    )
elif validator.type == "fmi":
    from validibot_shared.fmi import FMIInputEnvelope, FMIInputs
    envelope = FMIInputEnvelope(
        inputs=FMIInputs(...)  # Fully typed!
    )

# Upload to GCS
upload_envelope(envelope, input_uri)
```

### Validator Side (Processing Jobs)

```python
# Validator knows exact envelope type
from validibot_shared.energyplus.envelopes import EnergyPlusInputEnvelope

# Load with full type information
envelope = load_input_envelope(EnergyPlusInputEnvelope)

# envelope.inputs is typed as EnergyPlusInputs
timestep = envelope.inputs.timestep_per_hour  # IDE autocomplete works!
```

### Django Side (Receiving Results)

```python
# Django deserializes based on validator.type
run = ValidationRun.objects.get(id=callback.run_id)

if run.validator.type == "energyplus":
    from validibot_shared.energyplus import EnergyPlusOutputEnvelope
    output = download_envelope(callback.result_uri, EnergyPlusOutputEnvelope)
    # output.outputs is typed as EnergyPlusOutputs
    returncode = output.outputs.energyplus_returncode
```

## GCP Infrastructure

The authoritative deployment commands live in the repo `justfile`, plus:

- `docs/dev_docs/google_cloud/deployment.md` (Cloud Run services + `validibot.com` load balancer)
- `docs/dev_docs/validator_jobs_cloud_run.md` (validator jobs + callback flow)

At a high level:

- We deploy one Django image as two Cloud Run services (`validibot-web` and `validibot-worker`).
- We deploy validator containers as Cloud Run Jobs (`validibot-validator-energyplus`, `validibot-validator-fmi`, etc).
- The worker service is deployed with `--no-allow-unauthenticated` and only accepts authenticated calls.
- Cloud Tasks queues exist for future web→worker orchestration and retries, but validator jobs are triggered directly via the Jobs API today.

## Security

### Callback authentication via Cloud Run IAM

- The worker Cloud Run service is private and requires IAM authentication.
- Validator jobs run with a dedicated service account that has `roles/run.invoker`
  on the worker service.
- The callback client mints a Google-signed ID token from the metadata server
  (audience = callback URL) and includes it as the `Authorization: Bearer <token>`
  header.
- No JWT payload token or shared secret is exchanged in the envelope; IAM
  enforces trust.

Example:

```python
post_callback(
    callback_url=input_envelope.context.callback_url,
    run_id=input_envelope.run_id,
    status=ValidationStatus.SUCCESS,
    result_uri=output_uri,
)
```

## Monitoring

### Logging

All components log to Cloud Logging:

```python
import logging
logger = logging.getLogger(__name__)

# Django
logger.info("Triggered validator job", extra={
    "run_id": run.id,
    "validator_type": validator.type,
    "input_uri": input_uri
})

# Validator
logger.info("Simulation complete", extra={
    "run_id": run_id,
    "returncode": returncode,
    "duration_seconds": duration
})
```

### Metrics

Track in Cloud Monitoring:

- Job execution duration (histogram)
- Job success/failure rate (counter)
- Callback latency (histogram)
- Queue depth (gauge)

### Alerting

Alert on:

- High job failure rate (>5% in 5min)
- Long-running jobs (>1 hour)
- Callback failures (>3 in 5min)
- Queue backlog (>100 tasks)

## Adding New Validator Types

1. **Create envelope schemas** in `validibot_shared/{domain}/` (see `validibot_shared_dev/` in this workspace):

   ```python
   # validibot_shared/xml/envelopes.py
   class XMLInputs(BaseModel):
       schema_uri: str
       validation_mode: Literal["strict", "lenient"]

   class XMLInputEnvelope(ValidationInputEnvelope):
       inputs: XMLInputs
   ```

2. **Create validator container** in `validibot_validators` (see `validibot_validators_dev/validators/` in this workspace):

   - Copy `validators/energyplus/` as template
   - Update `Dockerfile` with domain-specific dependencies
   - Implement `runner.py` with validation logic
   - Update `main.py` to use your envelopes

3. **Deploy container** as a Cloud Run Job:

   ```bash
   just validator-deploy xml dev
   # or: just validators-deploy-all dev
   ```

4. **Update Django** to use new validator:
   ```python
   # In Django validator factory
   if validator.type == "xml":
       from validibot_shared.xml import XMLInputEnvelope, XMLInputs
       envelope = XMLInputEnvelope(inputs=XMLInputs(...))
   ```

---

## Self-Hosted Deployment

For self-hosted deployments (VPS, on-premise, any Docker-compatible environment),
validators run as Docker containers via the local Docker socket.

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Docker Host                                            │
│                                                         │
│  ┌──────────────────┐    ┌──────────────────────────┐  │
│  │  Django + Worker │    │  Validator Container     │  │
│  │                  │    │  (validibot-validator-X) │  │
│  │  - Web app       │───▶│                          │  │
│  │  - Dramatiq      │    │  Reads: file:///input    │  │
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

Required settings for self-hosted mode:

```python
# config/settings/self_hosted.py
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

### Docker Compose Setup

The `docker-compose.self-hosted.yml` file configures:

1. **Django service** with Docker socket mounted
2. **Dramatiq worker** for background task processing
3. **Shared storage volume** for file exchange
4. **Redis** for task queue broker

Key configuration:

```yaml
services:
  django:
    volumes:
      - validibot_storage:/app/storage
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - VALIDATOR_RUNNER=docker
      - DATA_STORAGE_ROOT=/app/storage/private
```

### Building Validator Images

Validator images must be available locally or in a registry:

```bash
# Build locally
cd ../validibot_validators
docker build -t validibot-validator-energyplus:latest validators/energyplus/

# Or pull from registry
docker pull your-registry/validibot-validator-energyplus:latest
```

### Execution Flow (Self-Hosted)

1. Engine creates `ExecutionRequest` with run, validator, submission, step
2. `SelfHostedExecutionBackend.execute()` is called
3. Backend writes input envelope to local storage (`file:///app/storage/...`)
4. Backend spawns Docker container with input/output URIs as environment variables
5. Container reads input, processes, writes output to local storage
6. Backend waits for container completion (synchronous)
7. Backend reads output envelope from local storage
8. Returns complete `ExecutionResponse` to engine

### Container Labels (Ryuk Pattern)

All validator containers are labeled for robust cleanup:

```
org.validibot.managed=true       # Identifies Validibot containers
org.validibot.run_id=<run-id>    # Validation run ID
org.validibot.validator=<slug>   # Validator slug (energyplus, fmi)
org.validibot.started_at=<iso>   # ISO timestamp
org.validibot.timeout_seconds=N  # Configured timeout
```

This enables three cleanup strategies:

1. **On-demand cleanup** - Container removed immediately after run completes
2. **Periodic sweep** - Background task removes orphaned containers every 10 minutes
3. **Startup cleanup** - Worker removes leftover containers from crashed workers on startup

### Container Cleanup

Orphaned containers can occur if a worker crashes mid-run. Use the management command:

```bash
# Show what would be cleaned up
python manage.py cleanup_containers --dry-run

# Remove orphaned containers (exceeded timeout + 5 min grace)
python manage.py cleanup_containers

# Remove ALL managed containers (for startup cleanup)
python manage.py cleanup_containers --all

# Custom grace period
python manage.py cleanup_containers --grace-period=600
```

The periodic cleanup task runs automatically every 10 minutes on self-hosted deployments.

### Debugging

Check container logs:

```bash
# List recent validator containers
docker ps -a --filter "name=validibot-validator"

# List Validibot-managed containers by label
docker ps -a --filter "label=org.validibot.managed=true"

# View container logs
docker logs <container-id>
```

Check storage:

```bash
# Inside Django container
ls -la /app/storage/private/runs/<org-id>/<run-id>/
cat /app/storage/private/runs/<org-id>/<run-id>/output.json
```
