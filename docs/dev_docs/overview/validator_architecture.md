# Validator Architecture

This document describes the complete architecture for running validations using Cloud Run Services and Cloud Run Jobs.

## Weather file catalog (EnergyPlus)

- Store all EPW files under a fixed, read-only prefix: `gs://<validation-bucket>/<GCS_WEATHER_PREFIX>/<file>.epw` (default prefix: `assets/weather`).
- Grant validator service accounts only `roles/storage.objectViewer` on that prefix (or bucket) so runtime code can read but not modify.
- Keep the catalog in the same region as the jobs to avoid cross-region egress.
- Optionally enable object versioning on the bucket for accidental delete/overwrite recovery; the job should still treat the catalog as immutable.
- Tests that need a weather file should verify it exists at the prefix (and may upload a fixture in non-prod) but production flows must only read from the catalog.

## Repository Structure

The validator system spans three codebases:

- **This repo (`validibot/`)**: the Django web + worker services (Cloud Run Services).
- **`vb_shared`**: shared Pydantic envelope models (installed here; see `vb_shared_dev/` for the local checkout).
- **`vb_validators`**: validator job containers (see `vb_validators_dev/` for the local checkout).

```
validibot/ (this repo)
├── validibot/                               # Django app (Cloud Run Services)
│   └── validations/
│       ├── services/cloud_run/              # Launcher + Jobs API client
│       └── api/callbacks.py                 # Worker-only callback endpoint
│
├── vb_shared_dev/                           # Local checkout of ../vb_shared (schemas + envelopes)
│   └── vb_shared/validations/envelopes.py   # ExecutionContext, ValidationCallback, etc.
│
└── vb_validators_dev/                       # Local checkout of ../vb_validators (Cloud Run Job containers)
    └── validators/
        ├── core/callback_client.py          # Posts callbacks (ID token)
        ├── energyplus/                      # EnergyPlus validator container
        └── fmi/                             # FMI validator container
```

## Data Flow

### 1. User Submits Model

```
User → Django REST API
  POST /api/v1/workflows/{id}/start/
  Body: IDF or epJSON content
```

### 2. Django Creates Validation Job

```python
# In Django (Cloud Run Service)
from vb_shared.energyplus import EnergyPlusInputEnvelope, EnergyPlusInputs
from vb_shared.validations.envelopes import InputFileItem, ExecutionContext

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
from vb_shared.energyplus.envelopes import EnergyPlusInputEnvelope, EnergyPlusOutputEnvelope
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
    from vb_shared.energyplus import EnergyPlusInputEnvelope, EnergyPlusInputs
    envelope = EnergyPlusInputEnvelope(
        inputs=EnergyPlusInputs(...)  # Fully typed!
    )
elif validator.type == "fmi":
    from vb_shared.fmi import FMIInputEnvelope, FMIInputs
    envelope = FMIInputEnvelope(
        inputs=FMIInputs(...)  # Fully typed!
    )

# Upload to GCS
upload_envelope(envelope, input_uri)
```

### Validator Side (Processing Jobs)

```python
# Validator knows exact envelope type
from vb_shared.energyplus.envelopes import EnergyPlusInputEnvelope

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
    from vb_shared.energyplus import EnergyPlusOutputEnvelope
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

1. **Create envelope schemas** in `vb_shared/{domain}/` (see `vb_shared_dev/` in this workspace):

   ```python
   # vb_shared/xml/envelopes.py
   class XMLInputs(BaseModel):
       schema_uri: str
       validation_mode: Literal["strict", "lenient"]

   class XMLInputEnvelope(ValidationInputEnvelope):
       inputs: XMLInputs
   ```

2. **Create validator container** in `vb_validators` (see `vb_validators_dev/validators/` in this workspace):

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
       from vb_shared.xml import XMLInputEnvelope, XMLInputs
       envelope = XMLInputEnvelope(inputs=XMLInputs(...))
   ```
