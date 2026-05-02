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

Create a `ValidatorConfig` to register your validator with the system. This is the
single source of truth for all validator metadata, class binding, and UI extensions.

**For package-based validators**, create `config.py` in your validator package:

```python
# validibot/validations/validators/myvalidator/config.py
from validibot.validations.validators.base.config import (
    CatalogEntrySpec,
    ValidatorConfig,
)

config = ValidatorConfig(
    slug="myvalidator",
    name="My Validator",
    description="Validates things using my custom logic.",
    validation_type="MY_VALIDATOR",
    validator_class="validibot.validations.validators.myvalidator.validator.MyValidator",
    has_processor=True,
    supported_file_types=["application/json"],
    catalog_entries=[
        CatalogEntrySpec(
            slug="result-metric",
            label="Result Metric",
            entry_type="signal",
            run_stage="output",
            data_type="number",
        ),
    ],
)
```

Then add your `ValidationType` to the enum and run `python manage.py sync_validators`
to sync to the database. The validator class is automatically resolved at startup by
`register_validators()`.

## Django-Side Orchestration (`AdvancedValidator`)

Before a container is started, the Django-side `AdvancedValidator` base class orchestrates the full validation lifecycle. This is the Template Method pattern — the base class handles shared orchestration while subclasses override hooks for domain-specific behavior.

### Lifecycle

```
AdvancedValidator.validate()
  1. Validate run_context (must have validation_run and step)
  2. Get the configured ExecutionBackend (Docker or Cloud Run)
  3. ★ preprocess_submission() — domain-specific input transformation
  4. Build ExecutionRequest and call backend.execute()
  5. Convert ExecutionResponse to ValidationResult
```

### Preprocessing Hook

`preprocess_submission()` is called **before** backend dispatch. This is where domain-specific input transformations happen — for example, EnergyPlus resolves parameterized IDF templates into concrete model files:

```python
# In EnergyPlusValidator
def preprocess_submission(self, *, step, submission):
    from .preprocessing import preprocess_energyplus_submission
    result = preprocess_energyplus_submission(step=step, submission=submission)
    return result.template_metadata
```

Key design principle: **preprocessing happens in Django, not in containers.** After preprocessing, the submission looks identical to a direct upload — execution backends never need to know that preprocessing occurred. This ensures all platforms (Docker Compose, Cloud Run, future backends) see identical input.

The default implementation is a no-op (returns empty dict). Subclasses override when needed.

### Subclass Hooks

| Method | Required | Purpose |
|--------|----------|---------|
| `validator_display_name` | Yes | Human-readable name for error messages |
| `extract_output_signals()` | Yes | Extract metrics from output envelope for assertions |
| `preprocess_submission()` | No | Transform submission before backend dispatch |

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

See the [validibot-validator-backends](https://github.com/danielmcquillen/validibot-validator-backends) repository for complete examples:

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

## The validator vs validator backend distinction

This document describes the **validator backend** interface — the external Docker image that does the heavyweight work. The Django-side code that orchestrates around it is an **advanced validator**. The two are different things, with different trust roles:

| Term | Meaning |
|---|---|
| **Validator** | The Django-side `BaseValidator` subclass. Sees full Validibot run context. |
| **Simple validator** | A `SimpleValidator` subclass that runs synchronously inside Django. No container. |
| **Advanced validator** | An `AdvancedValidator` subclass that orchestrates external compute via an execution backend. |
| **Validator backend** | The external implementation an advanced validator delegates to. **This page.** |
| **Validator backend runtime** | The concrete container/job launched for one run. |

**The advanced validator is the policy boundary; the validator backend is the compute boundary.** The advanced validator owns trust decisions (access, contract, retention, evidence). The backend just runs the simulation.

The repository was renamed from `validibot-validators` → `validibot-validator-backends` in March 2026 to make this role unambiguous. The Python package and Docker image prefixes match. See [Terminology](terminology.md) for the full vocabulary.

## Run-scoped isolation (Phase 1 of the trust ADR)

Before April 2026, the local Docker runner mounted the entire `DATA_STORAGE_ROOT` read-write into every validator backend runtime. A buggy or partner-authored backend could read other runs' inputs, mutate other runs' outputs, exhaust shared disk, or leak data between runs.

The trust ADR replaced that with a per-run workspace.

### Per-run workspace layout

```text
<DATA_STORAGE_ROOT>/runs/<org_id>/<run_id>/
  input/                       # mode 755 — readable by container UID 1000
    input.json                 # mode 644
    <original_filename>        # mode 644 — primary submission file
    resources/                 # mode 755
      <resource_filename>      # workflow resource files (e.g. weather)
  output/                      # owned 1000:1000, mode 770 — writable by UID 1000 only
    output.json                # written by container
    outputs/                   # backend-uploaded artifacts
```

### Container mounts

| Host path | Container path | Mode |
|---|---|---|
| `runs/<org_id>/<run_id>/input` | `/validibot/input` | read-only |
| `runs/<org_id>/<run_id>/output` | `/validibot/output` | read-write |
| (none) | `/tmp` | tmpfs (`size=2g,mode=1777`) |

The container does **not** receive the global storage root, other run directories, Django media paths, database credentials, signing keys, Stripe/x402 credentials, or arbitrary host directories.

### Envelope URI rewriting (no backend changes required)

The Docker dispatch path **rewrites three URI fields** in the input envelope so the container sees only container-visible paths:

- `input_files[].uri` → `file:///validibot/input/<filename>`
- `resource_files[].uri` → `file:///validibot/input/resources/<filename>`
- `context.execution_bundle_uri` → `file:///validibot/output`

Setting `execution_bundle_uri` to `file:///validibot/output` causes the backends' existing artifact-upload logic (which composes `f"{execution_bundle_uri}/outputs"`) to land artifacts at `/validibot/output/outputs/...`. **No validator-backend changes required** — backends already resolve URIs through their storage client.

The Cloud Run dispatch path is unchanged: it keeps `gs://...` URIs because each Cloud Run Job has its own GCS prefix and is naturally run-scoped.

### Workspace materialisation runs after preprocessing

Some advanced validators (notably EnergyPlus) preprocess the submission in-memory — for example, EnergyPlus template resolution rewrites `submission.content` and `submission.original_filename` before dispatch. The run-workspace builder must therefore run **after** the validator's `validate()` preprocessing has completed, i.e. inside `ExecutionBackend.execute()`.

### The implicit sentinel: how completion is detected

Validibot uses the **presence and parseability of `output.json`** as the implicit sentinel that says "the container ran and reported a result." Pattern adopted from Flyte (`_ERROR`), Cromwell (`rc` file), and Argo (exit code + artifact existence).

| State after container exit | Orchestrator interpretation |
|---|---|
| `output.json` present, parses, validator reports success | Run succeeded with the validator's verdict |
| `output.json` present, parses, validator reports validation failure | Run completed; user data did not pass |
| `output.json` present but unparseable | Backend bug; orchestrator records as `RuntimeError` |
| `output.json` absent + container exit code 0 | Backend bug; orchestrator records as `RuntimeError` |
| `output.json` absent + container exit code ≠ 0 | Container died unexpectedly (OOM, segfault, image pull, callback timeout); orchestrator records as `SystemError` |

This makes the boundary between **"validator reported the user's data is bad"** (a `FAIL` result the user can act on) and **"the platform itself had a problem"** (an `ERROR` the platform owner needs to triage) determinable from on-disk state alone, without reading container logs.

We do **not** add a separate `_status.json` file because doing so would require coordinated changes in `validibot-validator-backends`. The output envelope is already the artifact every backend produces; treating its presence/parseability as the sentinel is equivalent with zero coordination cost.

### Hardening posture vs comparable systems

Validibot's design closely mirrors Pachyderm's `/pfs/<input>` + `/pfs/out`, Flyte's `/var/inputs` + `/var/outputs`, and Cromwell's `/cromwell-executions/...` patterns. Validibot is **stricter than the median** on read-only inputs, default-deny network, non-root UID, and dropped capabilities. Most workflow engines (Argo, Flyte, Nextflow, Pachyderm, Airflow, Cromwell) leave these open by default.

## Validator backend metadata as security metadata

Validator backend metadata should describe runtime capabilities, not just UI metadata:

```json
{
  "slug": "energyplus",
  "version": "24.2.0",
  "supported_file_types": ["text", "json"],
  "requires_network": false,
  "requires_writeable_paths": ["/validibot/output", "/tmp"],
  "default_timeout_seconds": 900,
  "max_input_bytes": 104857600,
  "produces_artifacts": true,
  "evidence_level": "hashes-and-logs"
}
```

The runner enforces the deployment policy for capabilities it controls: network, writable paths, mounts, user, privileges, timeout, memory, CPU, root filesystem mode. A self-hosted deployment can choose "no validator network access" globally and refuse validators that request network. The runtime policy must not depend on trusting the image's self-description.

## Trust tiers (Phase 5, future)

A future `trust_tier` field on `Validator` will select a hardening profile:

- **Tier 1 — first-party** (EnergyPlus, FMU, future Validibot-built backends): current Phase 1 hardening.
- **Tier 2 — user-added or partner-authored**: tier 1 + explicit egress allowlist (or `network=none`), tighter resource caps, gVisor or Kata runtime when available, cosign-signed image required, pre-flight scan on registration.

Tier 1 defaults are sufficient for first-party containers Validibot builds itself. Tier 2 becomes load-bearing when self-service validator registration ships. See the [Trust Boundary Hardening ADR](https://github.com/danielmcquillen/validibot-project/blob/main/docs/adr/2026-04-27-trust-boundary-hardening-and-evidence-first-validation.md) Phase 5.

## See also

- [Terminology](terminology.md) — validator vs validator backend vs execution backend
- [Trust Architecture](trust-architecture.md) — the four invariants
- [Execution Backends](execution_backends.md) — Docker vs Cloud Run dispatch
- [Evidence Bundles](evidence-bundles.md) — what gets recorded about each validator backend run

