# ADR-2025-12-04: Validator Job Interface Contract

**Status:** Proposed
**Owner:** Platform / Validation Runtime
**Created:** 2025-12-04
**Related ADRs:** [2025-12-01: Google Cloud Platform Migration](2025-12-1-google-platform.md)

---

## 1. Context

Validibot is transitioning from Heroku + Celery + Modal.com to Google Cloud Run Services and Cloud Run Jobs (see [ADR-2025-12-01](2025-12-1-google-platform.md)).

We need a **standardized, future-proof contract** for how Django triggers validator jobs and how validator containers accept inputs and produce results.

Validators include heavy simulation engines (FMU, EnergyPlus) and will later include XML/JSON validators, AI models, PDF processors, and more.

### Key Requirements

- Support long-running simulations (hours to days)
- Handle multi-GB input/output files
- Extensible to new validator types without Django changes
- Clear contract for third-party or customer-authored validators
- Type safety and validation of inputs/outputs

### This ADR defines

- The universal **input envelope** (Django → Cloud Run Job)
- The universal **result envelope** (Job → Django)
- GCS as the shared I/O layer
- Rules for extensible validator types
- Security model for callbacks

---

## 2. Decision

We adopt a **single canonical contract** for communication between Django and all validator jobs, independent of validator type or implementation.

### Components included in this decision

1. **Input Envelope (`validibot.input.v1`)**
   - JSON document placed in GCS (`input.json`)
   - Lists validator type, job configuration, inputs, workflow step info, and callback metadata

2. **Result Envelope (`validibot.result.v1`)**
   - JSON document written by validator containers (`result.json`)
   - Unified fields for status, messages, metrics, artifacts, timing

3. **GCS Execution Bundle**
   ```
   gs://bucket/org_id/run_id/
       input.json
       <files uploaded by Django>
       result.json (created by job)
       <output files created by job>
   ```

4. **Cloud Run Job Execution Model**
   - Each validator type is a separate container image
   - Entry point reads `input.json`, pulls files, runs simulation, writes `result.json`
   - Job names follow convention: `validibot-validator-{type}` (e.g., `validibot-validator-energyplus`)

5. **Callback Model**
   - After writing results, the job POSTs a callback to the private worker service
   - Cloud Run IAM enforces authentication via a Google-signed ID token from the job's service account
   - Callback includes minimal payload: `run_id`, `status`, `result_uri`

6. **Extensibility**
   - Validator types are distinguished by `validator.type`
   - `inputs[*].role` allows type-specific conventions
   - Additional validator types must still emit standard result envelope fields

---

## 3. Input Envelope Specification

### Top-level structure

```jsonc
{
  "schema_version": "validibot.input.v1",
  "run_id": "uuid-string",
  "validator": {
    "id": "validator-uuid",
    "type": "energyplus" | "fmu" | "xml" | "pdf" | ...,
    "version": "semver-string"
  },
  "org": {
    "id": "org-uuid",
    "name": "Organization Name"
  },
  "workflow": {
    "id": "workflow-uuid",
    "step_id": "step-uuid",
    "step_name": "optional-human-readable"
  },
  "inputs": [
    // See "Input item format" below
  ],
  "config": {
    // Validator-specific configuration
    // Type validated by validator container
  },
  "context": {
    "callback_url": "https://validibot.com/api/v1/callbacks/validator",
    "execution_bundle_uri": "gs://bucket/org_id/run_id/",
    "timeout_seconds": 3600,
    "tags": ["tag1", "tag2"]
  }
}
```

### Input item format

```jsonc
{
  "name": "string",           // Human-readable name
  "kind": "file" | "json" | "string" | "number",
  "mime_type": "optional",    // e.g., "application/vnd.energyplus.idf"
  "role": "optional",         // Validator-specific role (see below)
  "uri": "gs://..." | null,   // For kind=file
  "value": {...} | null       // For kind=json/string/number
}
```

### Role conventions

Validators interpret `role` based on their type. Django does not need to understand these roles.

**EnergyPlus validator:**
- `primary-model` - The main IDF file
- `weather` - EPW weather file
- `config` - Additional configuration

**FMU validator:**
- `fmu` - The FMU file
- `config` - Simulation configuration
- `timeseries` - Input time series data

**Future validators:**
- XML: `xml-document`, `xml-schema`
- PDF: `pdf-document`, `validation-rules`
- AI models: `model`, `input-data`, `config`

### Security considerations

- Callback authentication is provided by Cloud Run IAM:
  - The worker service requires authentication.
  - Validator jobs use a dedicated service account with `roles/run.invoker` on the worker service.
  - The callback client fetches a Google-signed ID token (audience = callback URL) and sends it in `Authorization: Bearer <token>`.
- No shared secrets are exchanged in the envelope; callbacks rely on Google-signed ID tokens.

---

## 4. Result Envelope Specification

```jsonc
{
  "schema_version": "validibot.result.v1",
  "run_id": "uuid-string",
  "validator": {
    "id": "validator-uuid",
    "type": "energyplus",
    "version": "semver-string"
  },
  "status": "success" | "failed_validation" | "failed_runtime" | "cancelled",
  "timing": {
    "queued_at": "iso8601-timestamp",
    "started_at": "iso8601-timestamp",
    "finished_at": "iso8601-timestamp"
  },
  "messages": [
    // See "Messages" below
  ],
  "metrics": [
    // See "Metrics" below
  ],
  "artifacts": [
    // See "Artifacts" below
  ],
  "raw_outputs": {
    "format": "directory" | "archive",
    "manifest_uri": "gs://bucket/org_id/run_id/outputs/manifest.json"
  }
}
```

### Status values

- `success` - Validation completed successfully, no errors
- `failed_validation` - Validation found errors in the input (user's fault)
- `failed_runtime` - Runtime error in validator (system fault)
- `cancelled` - User or system cancelled the job

### Messages (standard across all validators)

Messages represent validation findings, warnings, and errors.

```jsonc
{
  "severity": "info" | "warning" | "error",
  "code": "optional-error-code",         // e.g., "EP001", "FMU_INIT_ERROR"
  "text": "Human-readable message",
  "location": {
    "file_role": "primary-model",        // References input role
    "line": 123,
    "column": 5,
    "path": "Zone/ZoneName"              // Object path or XPath-like
  },
  "tags": ["category1", "category2"]
}
```

**Example:**
```jsonc
{
  "severity": "error",
  "code": "EP_OBJECT_REQUIRED",
  "text": "Building object is required",
  "location": {
    "file_role": "primary-model",
    "line": 1,
    "column": 1
  },
  "tags": ["syntax", "required-object"]
}
```

### Metrics (bridge to signals)

Metrics represent computed values from the validation/simulation.

```jsonc
{
  "name": "zone_temp_max",
  "value": 28.5,
  "unit": "C",
  "category": "comfort",
  "tags": ["Zone1"]
}
```

**Common categories:**
- `comfort` - Thermal comfort metrics
- `energy` - Energy consumption
- `performance` - Simulation performance
- `compliance` - Code compliance checks

### Artifacts (large outputs)

Artifacts are files produced by the validator.

```jsonc
{
  "name": "simulation_db",
  "type": "simulation-db" | "report-html" | "timeseries-csv" | ...,
  "mime_type": "application/x-sqlite3",
  "uri": "gs://bucket/org_id/run_id/outputs/simulation.sql",
  "size_bytes": 12345678
}
```

**Common artifact types:**
- `simulation-db` - SQLite database with results
- `report-html` - HTML report
- `report-pdf` - PDF report
- `timeseries-csv` - Time series data
- `log-file` - Raw validator logs

---

## 5. Cloud Run Job Architecture

### Container structure

Each validator type is packaged as a container image:

```dockerfile
FROM python:3.11-slim

# Install validator-specific dependencies (e.g., EnergyPlus)
RUN apt-get update && apt-get install -y energyplus

# Install common library for envelope parsing
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy validator code
COPY validator/ /app/validator/
WORKDIR /app

# Entrypoint reads input.json and writes result.json
ENTRYPOINT ["python", "-m", "validator.main"]
```

### Execution flow

1. Django creates execution bundle in GCS:
   - Writes `input.json`
   - Uploads input files
   - Records callback URL for the worker service (protected by Cloud Run IAM)

2. Worker calls the Cloud Run Jobs API directly (fast, non-blocking):
   ```python
   from simplevalidations.validations.services.cloud_run import job_client

   execution_name = job_client.run_validator_job(
       project_id=project_id,
       region=region,
       job_name=f"validibot-validator-{validator_type}",  # short name, not fully-qualified
       input_uri=bundle_uri,
   )
   ```

3. Job container starts:
   - Reads `INPUT_URI` from environment (lightweight pointer; keeps large payload in GCS)
   - Downloads `input.json` from GCS
   - Validates input envelope schema
   - Downloads input files
   - Runs validator
   - Writes `result.json` to GCS
   - Uploads output artifacts
   - POSTs callback to Django

4. Django processes callback:
   - Verifies Google-signed ID token
   - Downloads `result.json` from GCS
   - Updates workflow status
   - Processes messages, metrics, artifacts
   - Triggers downstream workflow steps

### Resource limits

Jobs can specify resource requirements:

```python
# In Django validator configuration
resources = {
    "cpu": "4",           # 4 vCPUs
    "memory": "16Gi",     # 16 GB RAM
    "timeout": "1h"       # Maximum execution time
}
```

Cloud Run Jobs support:
- Up to 8 vCPUs per task
- Up to 32 GB RAM per task
- Up to 24 hours execution time (extended quota available)

---

## 6. Callback Security Model

### Token generation (Django)

No app-generated token is needed. Validator jobs mint a Google-signed ID token for the worker callback URL using their Cloud Run service account identity.

### Callback authentication (Django callback endpoint)

Cloud Run IAM validates the caller before Django code runs. The job presents a
Google-signed ID token (audience = callback URL) minted from its service
account; Django only needs to validate the payload fields.

```python
@api_view(['POST'])
def validator_callback(request):
    callback = ValidationCallback.model_validate(request.data)
    run_id = callback.run_id
    # Load result.json from GCS and process
```

### Callback payload

Minimal payload to avoid duplication:

```jsonc
{
  "run_id": "uuid-string",
  "status": "success",
  "result_uri": "gs://bucket/org_id/run_id/result.json"
}
```

Django loads the full `result.json` from GCS after verifying the token.

---

## 7. Schema Versioning

### Version negotiation

- Input envelope specifies `schema_version: "validibot.input.v1"`
- Validator containers check version and reject unsupported versions
- Result envelope echoes back `schema_version: "validibot.result.v1"`

### Evolution strategy

**Non-breaking changes** (patch/minor versions):
- Adding optional fields
- Adding new enum values
- Adding new validator types

**Breaking changes** (major versions):
- Removing required fields
- Changing field semantics
- Changing enum value meanings

When introducing `v2`:
- Django can write both `v1` and `v2` envelopes
- Validators declare supported versions in metadata
- Django selects appropriate version per validator

---

## 8. Why this approach

### Pros

- **Extremely extensible** — new validators drop in without Django changes
- **Minimal assumptions** — Django is a simple orchestrator
- **Cloud Run Jobs** allow heavy runtimes without HTTP timeout risk
- **GCS perfect for large files** — multi-GB artifacts handled naturally
- **Clear contract** for third-party or customer-authored validators
- **Type safety** via JSON schema + Pydantic models
- **Security** via Cloud Run IAM (Google-signed ID tokens; no shared secrets)

### Cons

- Slightly more GCS bookkeeping in Django
- Must maintain schema versioning for envelopes
- Validator containers need to follow spec strictly
- Additional latency from GCS round-trips (vs direct streaming)

### Mitigations

- Provide shared Python library for envelope parsing
- Provide validator base image with common dependencies
- Document schema evolution process
- Provide validator SDK with examples

---

## 9. Alternatives considered

### A. Direct gRPC streaming

**Rejected** — too complicated and hard to run inside serverless jobs. Would require long-lived connections and complex error handling.

### B. Sticking with Modal.com

**Rejected** — less control over infrastructure, cost scaling, and contract design. Vendor lock-in concerns.

### C. Pushing workflow logic inside Jobs

**Rejected** — Django must remain the orchestrator. Workflow state, user permissions, and organization context belong in Django.

### D. HTTP streaming with chunked transfer

**Rejected** — Cloud Run has hard timeout limits (60 min for HTTP). Long simulations would fail.

### E. Message queue (Cloud Pub/Sub) for callbacks

**Considered** — Could work well but adds complexity. HTTP callbacks with Cloud Run IAM ID tokens are simpler and more debuggable.

---

## 10. Migration Plan

### Phase 1: Foundation (Week 1-2)

1. ✅ Complete GCP platform migration
2. ✅ Finalize Cloud Run IAM callback authentication (ID tokens)
3. Create shared Pydantic models for input/output envelopes
4. Add GCS execution bundle builder to Django
5. Create validator callback endpoint with Cloud Run IAM ID token verification

### Phase 2: First validator (Week 3-4)

1. Build EnergyPlus validator container (v1)
2. Implement envelope parsing in container
3. Test end-to-end flow with sample IDF files
4. Document EnergyPlus-specific input roles

### Phase 3: Second validator (Week 5-6)

1. Build FMU validator container (v1)
2. Verify envelope contract works for different validator type
3. Extract common code into validator SDK

### Phase 4: Production rollout (Week 7-8)

1. Replace Modal tasks with Cloud Run Job triggers
2. Canary deployment with select users
3. Monitor performance and costs
4. Full production rollout

### Phase 5: Extensibility (Future)

1. Document third-party validator development
2. Build XML/JSON schema validator
3. Build AI model validator
4. Support customer-provided validators

---

## 11. Success Metrics

- All existing validators migrated from Modal.com
- Validation runs complete successfully with new architecture
- Job execution time comparable to Modal.com baseline
- Cost per validation run reduced by 30%+ vs Modal.com
- Zero callback authentication failures in production
- Third-party validator successfully integrated

---

## 12. Open Questions

1. **Retry strategy**: How should Django handle job failures? Automatic retry?
2. **Partial results**: Should validators support streaming partial results?
3. **Job prioritization**: How to handle queue priority for different orgs?
4. **Cost tracking**: How to attribute GCS/compute costs to orgs/workflows?

These will be addressed in follow-up ADRs as implementation proceeds.

---

## 13. References

- [ADR-2025-12-01: Google Cloud Platform Migration](2025-12-1-google-platform.md)
- [Cloud Run Jobs Documentation](https://cloud.google.com/run/docs/create-jobs)
- [Google Cloud Run IAM](https://cloud.google.com/run/docs/authenticating/service-to-service)

---

## Status & Next Steps

- **Status**: Proposed (pending implementation)
- **Next ADR**: Container base images, job invocation security, and IAM model
- **Implementation tracking**: [GitHub Project Board](https://github.com/org/repo/projects/1)
