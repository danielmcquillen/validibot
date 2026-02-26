# Advanced Validator Container Interface

Advanced validators run as isolated Docker containers, enabling complex domain-specific validation logic like simulations, external tools, or custom processing. This document describes the container interface that all advanced validators must implement.

For information about how Validibot orchestrates these containers across different deployment targets (Docker Compose, GCP Cloud Run, etc.), see [Execution Backends](execution_backends.md).

## Overview

An advanced validator is a Docker container that:

1. Reads an **input envelope** (JSON) describing what to validate
2. Performs validation (runs a simulation, calls external tools, etc.)
3. Writes an **output envelope** (JSON) with results

This simple contract makes it straightforward to package any validation logic as a container.

```
┌─────────────────────────────────────────────────────────────┐
│                   Validator Container                        │
│                                                              │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│   │ Read Input   │───▶│   Process    │───▶│ Write Output │  │
│   │   Envelope   │    │  Validation  │    │   Envelope   │  │
│   └──────────────┘    └──────────────┘    └──────────────┘  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
        ▲                                          │
        │                                          ▼
   input.json                                 output.json
```

## Environment Variables

Validibot passes configuration to containers via environment variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `VALIDIBOT_INPUT_URI` | URI to the input envelope | `file:///data/input.json` or `gs://bucket/run/input.json` |
| `VALIDIBOT_OUTPUT_URI` | URI where output envelope should be written | `file:///data/output.json` or `gs://bucket/run/output.json` |

The URI scheme indicates the storage backend:
- `file://` — Local filesystem (Docker Compose deployments)
- `gs://` — Google Cloud Storage (GCP deployments)

## Input Envelope

The input envelope is a JSON document that tells the validator what to do. It contains:

### Base Fields (all validators)

```python
from pydantic import BaseModel
from typing import Literal

class InputFileItem(BaseModel):
    """A file to be validated."""
    name: str                    # Filename (e.g., "model.idf")
    uri: str                     # Where to download the file
    mime_type: str               # MIME type for format detection
    role: str                    # Semantic role (e.g., "primary-model", "weather")

class ValidatorInfo(BaseModel):
    """Information about the validator being run."""
    id: str                      # Validator UUID
    type: str                    # Validator type (e.g., "energyplus", "fmu")
    version: str                 # Validator version

class ExecutionContext(BaseModel):
    """Execution metadata for callbacks and storage."""
    callback_url: str | None     # Where to POST results (async mode)
    callback_id: str | None      # Unique ID for idempotent callbacks
    execution_bundle_uri: str    # Base URI for storing artifacts
    timeout_seconds: int         # Maximum execution time

class ValidationInputEnvelope(BaseModel):
    """Base input envelope - extend for your validator."""
    run_id: str                  # Validation run UUID
    validator: ValidatorInfo
    input_files: list[InputFileItem]
    context: ExecutionContext
```

### Validator-Specific Inputs

Each validator type extends the base envelope with domain-specific configuration:

```python
# EnergyPlus validator
class EnergyPlusInputs(BaseModel):
    timestep_per_hour: int = 4
    output_variables: list[str] = []
    invocation_mode: Literal["cli", "api"] = "cli"

class EnergyPlusInputEnvelope(ValidationInputEnvelope):
    inputs: EnergyPlusInputs
```

```python
# FMU validator
class FMUInputs(BaseModel):
    start_time: float = 0.0
    stop_time: float = 1.0
    step_size: float | None = None

class FMUInputEnvelope(ValidationInputEnvelope):
    inputs: FMUInputs
```

### Example Input Envelope

```json
{
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "validator": {
    "id": "123e4567-e89b-12d3-a456-426614174000",
    "type": "energyplus",
    "version": "24.2.0"
  },
  "input_files": [
    {
      "name": "model.idf",
      "uri": "file:///data/files/model.idf",
      "mime_type": "application/vnd.energyplus.idf",
      "role": "primary-model"
    },
    {
      "name": "weather.epw",
      "uri": "file:///data/files/weather.epw",
      "mime_type": "application/vnd.energyplus.epw",
      "role": "weather"
    }
  ],
  "inputs": {
    "timestep_per_hour": 4,
    "output_variables": ["Zone Mean Air Temperature"],
    "invocation_mode": "cli"
  },
  "context": {
    "callback_url": null,
    "callback_id": null,
    "execution_bundle_uri": "file:///data/runs/550e8400/",
    "timeout_seconds": 3600
  }
}
```

## Output Envelope

The output envelope reports validation results back to Validibot.

### Base Fields (all validators)

```python
from enum import Enum

class ValidationStatus(str, Enum):
    SUCCESS = "success"       # Validation completed, model is valid
    FAILURE = "failure"       # Validation completed, model has issues
    ERROR = "error"           # Validation could not complete (crash, timeout)

class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

class ValidationMessage(BaseModel):
    """A message from the validation process."""
    severity: Severity
    text: str
    code: str | None = None     # Machine-readable error code
    location: str | None = None # File/line reference

class ValidationMetric(BaseModel):
    """A numeric metric extracted during validation."""
    name: str                   # Metric identifier
    value: float | int | str    # Metric value
    unit: str | None = None     # Unit of measurement

class ValidationOutputEnvelope(BaseModel):
    """Base output envelope - extend for your validator."""
    run_id: str
    validator: ValidatorInfo
    status: ValidationStatus
    timing: dict                # started_at, finished_at timestamps
    messages: list[ValidationMessage] = []
    metrics: list[ValidationMetric] = []
```

### Validator-Specific Outputs

Each validator type can include domain-specific output data:

```python
# EnergyPlus validator
class EnergyPlusOutputs(BaseModel):
    energyplus_returncode: int
    execution_seconds: float
    invocation_mode: str
    # References to output files (SQL, CSV, etc.)

class EnergyPlusOutputEnvelope(ValidationOutputEnvelope):
    outputs: EnergyPlusOutputs
```

### Example Output Envelope

```json
{
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "validator": {
    "id": "123e4567-e89b-12d3-a456-426614174000",
    "type": "energyplus",
    "version": "24.2.0"
  },
  "status": "success",
  "timing": {
    "started_at": "2024-01-15T10:30:00Z",
    "finished_at": "2024-01-15T10:35:42Z"
  },
  "messages": [
    {
      "severity": "info",
      "text": "Simulation completed successfully"
    },
    {
      "severity": "warning",
      "text": "Zone 'Kitchen' has no windows",
      "code": "EP_NO_WINDOWS",
      "location": "model.idf:1523"
    }
  ],
  "metrics": [
    {
      "name": "total_site_energy_kwh",
      "value": 45230.5,
      "unit": "kWh"
    },
    {
      "name": "simulation_time_seconds",
      "value": 342,
      "unit": "s"
    }
  ],
  "outputs": {
    "energyplus_returncode": 0,
    "execution_seconds": 342.5,
    "invocation_mode": "cli"
  }
}
```

## Container Labels

Validibot identifies and manages validator containers using Docker labels:

| Label | Description | Example |
|-------|-------------|---------|
| `org.validibot.managed` | Marks container as Validibot-managed | `true` |
| `org.validibot.run_id` | Validation run UUID | `550e8400-e29b-...` |
| `org.validibot.validator` | Validator slug | `energyplus` |
| `org.validibot.started_at` | ISO timestamp when started | `2024-01-15T10:30:00Z` |
| `org.validibot.timeout_seconds` | Configured timeout | `3600` |

These labels enable:

1. **On-demand cleanup** — Container removed after run completes
2. **Periodic sweep** — Background task removes orphaned containers
3. **Startup cleanup** — Worker removes leftover containers from crashed processes

## Building Your Own Validator

### 1. Define Envelope Schemas

Create Pydantic models in `validibot-shared` for your validator's inputs and outputs:

```python
# validibot_shared/myvalidator/envelopes.py
from validibot_shared.validations.envelopes import (
    ValidationInputEnvelope,
    ValidationOutputEnvelope,
)

class MyValidatorInputs(BaseModel):
    setting_a: str
    setting_b: int = 10

class MyValidatorInputEnvelope(ValidationInputEnvelope):
    inputs: MyValidatorInputs

class MyValidatorOutputs(BaseModel):
    result_code: int
    summary: str

class MyValidatorOutputEnvelope(ValidationOutputEnvelope):
    outputs: MyValidatorOutputs
```

### 2. Create the Container

Structure your validator container:

```
myvalidator/
├── Dockerfile
├── main.py           # Entry point
├── runner.py         # Validation logic
└── requirements.txt
```

**Dockerfile:**

```dockerfile
FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "main.py"]
```

**main.py:**

```python
import os
import json
from myvalidator.runner import run_validation
from validibot_shared.myvalidator.envelopes import (
    MyValidatorInputEnvelope,
    MyValidatorOutputEnvelope,
)

def main():
    # Read input envelope
    input_uri = os.environ["VALIDIBOT_INPUT_URI"]
    output_uri = os.environ["VALIDIBOT_OUTPUT_URI"]

    with open(input_uri.replace("file://", "")) as f:
        input_envelope = MyValidatorInputEnvelope.model_validate_json(f.read())

    # Run validation
    output_envelope = run_validation(input_envelope)

    # Write output envelope
    with open(output_uri.replace("file://", ""), "w") as f:
        f.write(output_envelope.model_dump_json(indent=2))

if __name__ == "__main__":
    main()
```

**runner.py:**

```python
from datetime import datetime, timezone
from validibot_shared.validations.envelopes import (
    ValidationStatus,
    ValidationMessage,
    Severity,
)

def run_validation(input_envelope):
    started_at = datetime.now(timezone.utc)

    # Download input files
    for file_item in input_envelope.input_files:
        download_file(file_item.uri, f"/tmp/{file_item.name}")

    # Your validation logic here
    # ...

    finished_at = datetime.now(timezone.utc)

    return MyValidatorOutputEnvelope(
        run_id=input_envelope.run_id,
        validator=input_envelope.validator,
        status=ValidationStatus.SUCCESS,
        timing={
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
        },
        messages=[
            ValidationMessage(
                severity=Severity.INFO,
                text="Validation completed successfully",
            )
        ],
        metrics=[],
        outputs=MyValidatorOutputs(
            result_code=0,
            summary="All checks passed",
        ),
    )
```

### 3. Register in Validibot

Update the Django code to recognize your validator type:

```python
# In the validator factory/registry
if validator.type == "myvalidator":
    from validibot_shared.myvalidator import MyValidatorInputEnvelope
    envelope_class = MyValidatorInputEnvelope
```

## Container Lifecycle

A validator container goes through these stages:

### Sync Path (Docker Compose)

```
1. SPAWN    → DockerValidatorRunner.run() creates container with labels
              Security: cap_drop=ALL, no-new-privileges, read_only, network_mode=none
2. EXECUTE  → Container reads input from VALIDIBOT_INPUT_URI, runs validation
3. WAIT     → Worker blocks on container.wait(timeout=N)
4. COLLECT  → Worker reads output.json from shared storage volume
5. CLEANUP  → container.remove(force=True) in finally block
```

**If the worker crashes mid-execution:** The container becomes an orphan. It is cleaned up by:
- Periodic sweep (Celery Beat every 10 minutes) — checks container labels for timeout
- Startup cleanup (AppConfig.ready()) — removes all labeled containers on worker restart

### Async Path (GCP Cloud Run)

```
1. UPLOAD   → GCPExecutionBackend uploads input envelope + files to GCS
2. TRIGGER  → Cloud Run Jobs API starts execution (returns immediately)
3. EXECUTE  → Container runs on Cloud Run (minutes to hours)
4. CALLBACK → Container POSTs callback to Django worker with result_uri
5. PROCESS  → ValidationCallbackService downloads output, processes results
```

**If the callback is lost:** The `cleanup_stuck_runs` command queries the Cloud Run Jobs API:
- Job succeeded → Recovers results via synthetic callback through `ValidationCallbackService`
- Job failed → Marks run as `FAILED` with the error message
- Job still running → Skips (no false timeout)
- API unavailable → Falls through to simple `TIMED_OUT` marking

### Container Security Hardening

All Docker containers are launched with these security settings:

| Setting | Value | Purpose |
|---------|-------|---------|
| `cap_drop` | `["ALL"]` | Drop all Linux capabilities |
| `security_opt` | `["no-new-privileges:true"]` | Prevent privilege escalation |
| `pids_limit` | `512` | Prevent fork bombs |
| `read_only` | `True` | Read-only root filesystem |
| `tmpfs` | `{"/tmp": "size=2g,mode=1777"}` | Writable /tmp only |
| `user` | `"1000:1000"` | Run as non-root |
| `network_mode` | `"none"` | No network access (sync mode) |
| `mem_limit` | `"4g"` (configurable) | Memory ceiling |
| `nano_cpus` | `2_000_000_000` (configurable) | CPU ceiling |

## Reference Implementations

See the [validibot-validators](https://github.com/danielmcquillen/validibot-validators) repository for complete examples:

- **EnergyPlus validator** — Building energy simulation
- **FMU validator** — Functional Mock-up Unit simulation

## Type Safety

The envelope schemas are defined in `validibot-shared` and used by both Validibot (Django) and the validator containers. This ensures:

- **Compile-time validation** — Type errors caught before runtime
- **IDE support** — Autocomplete and type hints work correctly
- **Schema evolution** — Changes to envelopes are versioned and tested

```python
# Django side - creating jobs
envelope = EnergyPlusInputEnvelope(
    inputs=EnergyPlusInputs(timestep_per_hour=6)  # Fully typed!
)

# Validator side - processing jobs
envelope = load_input_envelope(EnergyPlusInputEnvelope)
timestep = envelope.inputs.timestep_per_hour  # IDE knows this is int
```
