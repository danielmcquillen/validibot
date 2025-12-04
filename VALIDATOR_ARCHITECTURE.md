# Validator Architecture

This document describes the complete architecture for running validations using Cloud Run Services and Cloud Run Jobs.

## Repository Structure

```
validibot/
├── validibot/                    # Django app (Cloud Run Service)
│   ├── simplevalidations/        # Main Django project
│   └── ...
│
├── sv_shared/                    # Shared schemas (Python package)
│   ├── validations/
│   │   └── envelopes.py         # Base input/output envelopes
│   ├── energyplus/
│   │   ├── models.py            # EnergyPlus output models
│   │   └── envelopes.py         # EnergyPlus typed envelopes
│   └── fmi/
│       └── models.py            # FMI models
│
└── validators/                   # Cloud Run Job validators
    ├── shared/                  # Shared utilities for all validators
    │   ├── gcs_client.py        # GCS download/upload helpers
    │   ├── callback_client.py   # HTTP callback utilities
    │   └── envelope_loader.py   # Envelope loading helpers
    │
    └── energyplus/              # EnergyPlus validator container
        ├── Dockerfile
        ├── main.py              # Entrypoint
        ├── runner.py            # EnergyPlus execution logic
        ├── requirements.txt
        └── tests/
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
from sv_shared.energyplus import EnergyPlusInputEnvelope, EnergyPlusInputs
from sv_shared.validations.envelopes import InputFileItem, ExecutionContext

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
        callback_url=HttpUrl("https://validibot.example.com/api/v1/validation-callbacks/"),
        callback_token=create_jwt_token(run.id),
        execution_bundle_uri=f"gs://bucket/{org.id}/{run.id}/",
        timeout_seconds=3600
    )
)

# Upload input envelope to GCS
input_uri = f"gs://bucket/{org.id}/{run.id}/input.json"
upload_envelope(input_envelope, input_uri)

# Trigger Cloud Run Job via Cloud Tasks
create_cloud_task(
    queue="validator-jobs",
    url=f"https://{REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{PROJECT}/jobs/validibot-validator-energyplus:run",
    payload={
        "overrides": {
            "containerOverrides": [{
                "env": [
                    {"name": "INPUT_URI", "value": input_uri}
                ]
            }]
        }
    }
)
```

### 3. Cloud Run Job Executes

```python
# In validators/energyplus/main.py
from sv_shared.energyplus.envelopes import EnergyPlusInputEnvelope, EnergyPlusOutputEnvelope
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
    callback_token=input_envelope.context.callback_token,
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
    # Validate JWT token
    callback = ValidationCallback.model_validate(request.data)
    verify_jwt_token(callback.callback_token)

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
    from sv_shared.energyplus import EnergyPlusInputEnvelope, EnergyPlusInputs
    envelope = EnergyPlusInputEnvelope(
        inputs=EnergyPlusInputs(...)  # Fully typed!
    )
elif validator.type == "fmi":
    from sv_shared.fmi import FMIInputEnvelope, FMIInputs
    envelope = FMIInputEnvelope(
        inputs=FMIInputs(...)  # Fully typed!
    )

# Upload to GCS
upload_envelope(envelope, input_uri)
```

### Validator Side (Processing Jobs)

```python
# Validator knows exact envelope type
from sv_shared.energyplus.envelopes import EnergyPlusInputEnvelope

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
    from sv_shared.energyplus import EnergyPlusOutputEnvelope
    output = download_envelope(callback.result_uri, EnergyPlusOutputEnvelope)
    # output.outputs is typed as EnergyPlusOutputs
    returncode = output.outputs.energyplus_returncode
```

## GCP Infrastructure

### Cloud Run Service (Django)

```bash
gcloud run deploy validibot \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --service-account django-runner@PROJECT.iam.gserviceaccount.com
```

**Service Account Permissions:**
- `roles/cloudstorage.objectAdmin` - Upload/download GCS files
- `roles/cloudtasks.enqueuer` - Create Cloud Tasks
- `roles/run.admin` - Trigger Cloud Run Jobs
- `roles/cloudkms.cryptoKeyEncrypterDecrypter` - Sign/verify JWT tokens

### Cloud Run Jobs (Validators)

```bash
# Build container
gcloud builds submit \
  --tag gcr.io/PROJECT/validibot-validator-energyplus \
  validators/energyplus

# Create job
gcloud run jobs create validibot-validator-energyplus \
  --image gcr.io/PROJECT/validibot-validator-energyplus \
  --region us-central1 \
  --memory 4Gi \
  --cpu 2 \
  --max-retries 0 \
  --task-timeout 3600 \
  --service-account validator-runner@PROJECT.iam.gserviceaccount.com
```

**Service Account Permissions:**
- `roles/cloudstorage.objectAdmin` - Download inputs, upload outputs
- `roles/run.invoker` - Self-invoke for retries (optional)

### Cloud Tasks Queue

```bash
gcloud tasks queues create validator-jobs \
  --location us-central1 \
  --max-dispatches-per-second 10 \
  --max-concurrent-dispatches 100
```

### GCS Bucket

```bash
gsutil mb -l us-central1 gs://PROJECT-validator-bundles
gsutil lifecycle set lifecycle.json gs://PROJECT-validator-bundles
```

**Lifecycle policy (lifecycle.json):**
```json
{
  "lifecycle": {
    "rule": [{
      "action": {"type": "Delete"},
      "condition": {"age": 30}
    }]
  }
}
```

## Security

### JWT Tokens for Callbacks

```python
# Django creates signed token
import jwt
from google.cloud import kms

def create_callback_token(run_id: str) -> str:
    payload = {
        "run_id": run_id,
        "exp": datetime.now(UTC) + timedelta(hours=24),
        "iss": "validibot-django",
    }

    # Sign with GCP KMS
    kms_client = kms.KeyManagementServiceClient()
    key_name = f"projects/{PROJECT}/locations/{REGION}/keyRings/{KEYRING}/cryptoKeys/{KEY}"

    # Create JWT and sign with KMS
    token = jwt.encode(payload, key=None, algorithm="RS256")
    signature = kms_client.asymmetric_sign(
        request={"name": f"{key_name}/cryptoKeyVersions/1", "data": token.encode()}
    )

    return f"{token}.{signature.signature}"

# Validator includes token in callback
post_callback(
    callback_url=callback_url,
    callback_token=token,
    ...
)

# Django verifies token
def verify_callback_token(token: str) -> dict:
    # Verify with GCP KMS public key
    kms_client = kms.KeyManagementServiceClient()
    public_key = kms_client.get_public_key(...)

    payload = jwt.decode(token, public_key, algorithms=["RS256"])
    return payload
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

1. **Create envelope schemas** in `sv_shared/{domain}/`:
   ```python
   # sv_shared/xml/envelopes.py
   class XMLInputs(BaseModel):
       schema_uri: str
       validation_mode: Literal["strict", "lenient"]

   class XMLInputEnvelope(ValidationInputEnvelope):
       inputs: XMLInputs
   ```

2. **Create validator container** in `validators/{domain}/`:
   - Copy `validators/energyplus/` as template
   - Update `Dockerfile` with domain-specific dependencies
   - Implement `runner.py` with validation logic
   - Update `main.py` to use your envelopes

3. **Deploy container** as Cloud Run Job:
   ```bash
   gcloud builds submit --tag gcr.io/PROJECT/validibot-validator-xml validators/xml
   gcloud run jobs create validibot-validator-xml --image gcr.io/PROJECT/validibot-validator-xml ...
   ```

4. **Update Django** to use new validator:
   ```python
   # In Django validator factory
   if validator.type == "xml":
       from sv_shared.xml import XMLInputEnvelope, XMLInputs
       envelope = XMLInputEnvelope(inputs=XMLInputs(...))
   ```
