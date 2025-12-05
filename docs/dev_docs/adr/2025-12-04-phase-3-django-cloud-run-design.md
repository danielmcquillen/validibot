# ADR: Phase 3 Django Cloud Run Infrastructure Design

**Date:** 2025-12-04
**Status:** Implemented
**Context:** Greenfield implementation of Google Cloud Run Jobs for advanced validations

**NOTE:** This is NOT a migration from Modal.com. The site is not yet live, so we build Cloud Run Jobs as the only validator execution system.

## Summary

This ADR proposes the Django-side architecture for triggering Cloud Run Job validators and receiving their results. The design prioritizes simplicity, testability, and clear separation of concerns.

## Deployment Roles (web vs worker)

We run the same Django image as two Cloud Run services:

- `APP_ROLE=web` (public): marketing + UI only. No API routes are loaded.
- `APP_ROLE=worker` (private/IAM): API-only surface, including `/api/v1/validation-callbacks/`. Cloud Run IAM `roles/run.invoker` on the worker service gates access; validator jobs call back with an ID token minted from their service account.

Implications:
- `ENABLE_API` is ignored unless `APP_ROLE=worker`.
- The callback view 404s on non-worker instances as a defense-in-depth guard.
- Deploy web with `--allow-unauthenticated APP_ROLE=web`; deploy worker with `--no-allow-unauthenticated APP_ROLE=worker`.

## Design Principles

Per AGENTS.md and CLAUDE.md:
- **Verbose and simple over clever tricks** - Straightforward code is better than complex patterns
- **Elegant but not over-verbose** - Balance clarity with conciseness
- **Django patterns** - Use standard Django approaches, document any advanced techniques
- **Testable** - Each component should be easy to unit test
- **Google Python guidelines** - All classes have docstrings with context

## Architecture Overview

```
User submits model
    ↓
Validation Engine (simplevalidations/validations/engines/)
    ↓
Cloud Run Service (new: simplevalidations/validations/services/cloud_run/)
    ├── envelope_builder.py    # Creates typed input envelopes
    ├── gcs_client.py          # Uploads/downloads envelopes to GCS
    ├── job_client.py          # Triggers Cloud Run Jobs via Cloud Tasks
    └── token_service.py       # Creates JWT callback tokens
    ↓
Cloud Run Job Validator (validators/energyplus/)
    ↓
Callback (new: simplevalidations/validations/api/callbacks.py)
    ↓
Database updated with results
```

## Component Design

### 1. Envelope Builder (`envelope_builder.py`)

**Purpose:** Create typed input envelopes based on validator type.

**Design Decision:** Use a simple factory function, not a class hierarchy. Each validator type has a dedicated builder function.

```python
"""
Envelope builder for creating typed validation input envelopes.

This module provides functions to build domain-specific input envelopes
(EnergyPlusInputEnvelope, FMIInputEnvelope, etc.) from Django model instances.

Design: Simple factory functions, not classes. Each validator type gets its own
builder function. This keeps the code straightforward and easy to test.
"""

from typing import Protocol
from sv_shared.energyplus.envelopes import EnergyPlusInputEnvelope, EnergyPlusInputs
from sv_shared.validations.envelopes import ValidationInputEnvelope


class ValidatorLike(Protocol):
    """Protocol for validator-like objects (duck typing for easier testing)."""
    id: str
    type: str
    version: str


def build_energyplus_input_envelope(
    *,
    run_id: str,
    validator: ValidatorLike,
    org_id: str,
    org_name: str,
    workflow_id: str,
    step_id: str,
    step_name: str | None,
    model_file_uri: str,
    weather_file_uri: str,
    callback_url: str,
    callback_token: str,
    execution_bundle_uri: str,
    timestep_per_hour: int = 4,
    output_variables: list[str] | None = None,
) -> EnergyPlusInputEnvelope:
    """
    Build an EnergyPlusInputEnvelope from Django validation run data.

    This function creates a fully typed input envelope for EnergyPlus validators.
    It takes Django model data and transforms it into the Cloud Run Job input format.

    Args:
        run_id: Validation run UUID
        validator: Validator instance (or validator-like object)
        org_id: Organization UUID
        org_name: Organization name (for logging)
        workflow_id: Workflow UUID
        step_id: Workflow step UUID
        step_name: Human-readable step name
        model_file_uri: GCS URI to IDF/epJSON file
        weather_file_uri: GCS URI to EPW weather file
        callback_url: Django endpoint to POST results
        callback_token: JWT token for callback authentication
        execution_bundle_uri: GCS directory for this run's files
        timestep_per_hour: EnergyPlus timesteps (default: 4)
        output_variables: EnergyPlus output variables to collect

    Returns:
        Fully populated EnergyPlusInputEnvelope ready for GCS upload

    Example:
        >>> envelope = build_energyplus_input_envelope(
        ...     run_id=str(run.id),
        ...     validator=run.validator,
        ...     org_id=str(run.org.id),
        ...     org_name=run.org.name,
        ...     # ... other args
        ... )
        >>> # Upload to GCS and trigger job
    """
    # Implementation here - straightforward data transformation
    pass


def build_input_envelope(
    run,  # ValidationRun instance
    callback_url: str,
    callback_token: str,
) -> ValidationInputEnvelope:
    """
    Build the appropriate input envelope based on validator type.

    This is the main entry point for envelope creation. It dispatches to
    type-specific builders based on run.validator.type.

    Args:
        run: ValidationRun Django model instance
        callback_url: Django callback endpoint URL
        callback_token: JWT token for callback authentication

    Returns:
        Typed envelope (EnergyPlusInputEnvelope, FMIInputEnvelope, etc.)

    Raises:
        ValueError: If validator type is not supported
    """
    if run.validator.type == "energyplus":
        return build_energyplus_input_envelope(...)
    elif run.validator.type == "fmi":
        return build_fmi_input_envelope(...)
    else:
        raise ValueError(f"Unsupported validator type: {run.validator.type}")
```

**Why this design:**
- Simple function calls, easy to understand
- Each builder is independent and testable
- Type hints make it clear what data is needed
- No complex inheritance or metaclasses
- Easy to add new validator types

### 2. GCS Client (`gcs_client.py`)

**Purpose:** Upload/download envelopes to Google Cloud Storage.

**Design Decision:** Thin wrapper around google-cloud-storage with validation-specific helpers.

```python
"""
Google Cloud Storage client for validation envelopes.

This module provides functions to upload and download validation envelopes
to/from GCS. It's a thin, focused wrapper around google-cloud-storage.

Design: Simple functions that do one thing well. No stateful client objects.
"""

from pathlib import Path
from google.cloud import storage
from pydantic import BaseModel


def upload_envelope(
    envelope: BaseModel,
    uri: str,
) -> None:
    """
    Upload a Pydantic envelope to GCS as JSON.

    Args:
        envelope: Any Pydantic model (ValidationInputEnvelope, etc.)
        uri: GCS URI (e.g., 'gs://bucket/org_id/run_id/input.json')

    Raises:
        ValueError: If URI is invalid
        google.cloud.exceptions.GoogleCloudError: If upload fails
    """
    # Parse URI, serialize envelope, upload
    pass


def download_envelope(
    uri: str,
    envelope_class: type[BaseModel],
) -> BaseModel:
    """
    Download and deserialize a Pydantic envelope from GCS.

    Args:
        uri: GCS URI to the envelope JSON
        envelope_class: Pydantic model class to deserialize to

    Returns:
        Deserialized envelope instance

    Raises:
        ValueError: If URI is invalid or file doesn't exist
        ValidationError: If JSON doesn't match envelope schema
    """
    # Download, deserialize, return
    pass


def upload_file(
    local_path: Path,
    uri: str,
    content_type: str | None = None,
) -> None:
    """
    Upload a local file to GCS.

    Args:
        local_path: Path to local file
        uri: GCS URI destination
        content_type: Optional MIME type

    Raises:
        FileNotFoundError: If local file doesn't exist
        google.cloud.exceptions.GoogleCloudError: If upload fails
    """
    pass
```

**Why this design:**
- Functions, not classes - simpler to test and use
- Clear single responsibility
- Reuses existing patterns from validators/shared/gcs_client.py
- Easy to mock for testing

### 3. Job Client (`job_client.py`)

**Purpose:** Trigger Cloud Run Jobs via Cloud Tasks.

**Design Decision:** Use Cloud Tasks for async job triggering, not direct Cloud Run Jobs API.

**Why Cloud Tasks:**
- Built-in retry logic
- Rate limiting
- Better observability
- Decouples Django from job execution

```python
"""
Cloud Run Job client using Cloud Tasks for async execution.

This module triggers Cloud Run Jobs by creating Cloud Tasks that invoke
the jobs. This provides better retry logic and decoupling than calling
the Cloud Run Jobs API directly.

Design: Simple functions that create tasks. No complex state management.
"""

from google.cloud import tasks_v2


def trigger_validator_job(
    *,
    project_id: str,
    region: str,
    queue_name: str,
    job_name: str,
    input_uri: str,
) -> str:
    """
    Trigger a Cloud Run Job by creating a Cloud Task.

    This function creates a task that will execute the specified Cloud Run Job
    with the given input URI as an environment variable.

    Args:
        project_id: GCP project ID
        region: GCP region (e.g., 'us-central1')
        queue_name: Cloud Tasks queue name (e.g., 'validator-jobs')
        job_name: Cloud Run Job name (e.g., 'validibot-validator-energyplus')
        input_uri: GCS URI to input.json

    Returns:
        Task name (can be used for tracking)

    Raises:
        google.cloud.exceptions.GoogleCloudError: If task creation fails

    Example:
        >>> task_name = trigger_validator_job(
        ...     project_id="my-project",
        ...     region="us-central1",
        ...     queue_name="validator-jobs",
        ...     job_name="validibot-validator-energyplus",
        ...     input_uri="gs://bucket/org/run/input.json",
        ... )
    """
    # Create task with Cloud Run Job target
    pass
```

**Why this design:**
- Cloud Tasks handles retries automatically
- Better observability via Cloud Tasks console
- Can control rate limiting per queue
- Simple function call, not a complex client object

### 4. Token Service (`token_service.py`)

**Purpose:** Create JWT callback tokens signed with GCP KMS.

**Design Decision:** Use GCP KMS for signing, not local secrets.

```python
"""
JWT token service using GCP KMS for signing.

This module creates JWT tokens for validator callbacks. Tokens are signed
using GCP KMS asymmetric keys, avoiding the need to store signing secrets
in the Django app.

Design: Simple functions for create/verify. No stateful objects.
"""

from datetime import datetime, timedelta, UTC
from google.cloud import kms
import jwt


def create_callback_token(
    *,
    run_id: str,
    kms_key_name: str,
    expires_hours: int = 24,
) -> str:
    """
    Create a JWT callback token signed with GCP KMS.

    Args:
        run_id: Validation run UUID
        kms_key_name: Full KMS key path (projects/.../keys/...)
        expires_hours: Token expiration in hours (default: 24)

    Returns:
        JWT token string

    Example:
        >>> token = create_callback_token(
        ...     run_id="abc-123",
        ...     kms_key_name="projects/my-project/locations/us/keyRings/..."
        ... )
    """
    pass


def verify_callback_token(
    token: str,
    kms_key_name: str,
) -> dict:
    """
    Verify a JWT callback token using GCP KMS public key.

    Args:
        token: JWT token string
        kms_key_name: Full KMS key path

    Returns:
        Token payload dict with 'run_id', 'exp', etc.

    Raises:
        jwt.InvalidTokenError: If token is invalid or expired
    """
    pass
```

### 5. Callback API Endpoint (`api/callbacks.py`)

**Purpose:** Receive ValidationCallback POSTs from Cloud Run Jobs.

**Design Decision:** Simple DRF APIView, not a complex viewset.

```python
"""
Callback API endpoint for Cloud Run Job validators.

This module handles ValidationCallback POSTs from validator containers.
It verifies the JWT token, downloads the output envelope from GCS, and
updates the ValidationRun in the database.

Design: Simple APIView with clear error handling. No complex permissions.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from sv_shared.validations.envelopes import ValidationCallback


class ValidationCallbackView(APIView):
    """
    Handle validation completion callbacks from Cloud Run Jobs.

    This endpoint receives POSTs from validator containers when they finish
    executing. The callback contains minimal data (run_id, status, result_uri)
    and a JWT token for authentication.

    The endpoint:
    1. Verifies the JWT token
    2. Downloads the full output envelope from GCS
    3. Updates the ValidationRun in the database
    4. Returns 200 OK

    URL: /api/v1/validation-callbacks/
    Method: POST
    Authentication: JWT token in callback payload
    """

    def post(self, request):
        """Handle validation callback from Cloud Run Job."""
        # 1. Parse and validate callback
        # 2. Verify JWT token
        # 3. Download output envelope from GCS
        # 4. Update ValidationRun
        # 5. Return success
        pass
```

## Testing Strategy

Each component is designed to be easily testable:

```python
# Test envelope builder
def test_build_energyplus_input_envelope():
    """Test that envelope builder creates correct structure."""
    envelope = build_energyplus_input_envelope(
        run_id="test-123",
        # ... test data
    )
    assert envelope.run_id == "test-123"
    assert envelope.validator.type == "energyplus"

# Test GCS client with mocks
def test_upload_envelope(mock_storage_client):
    """Test envelope upload to GCS."""
    envelope = EnergyPlusInputEnvelope(...)
    upload_envelope(envelope, "gs://test/input.json")
    mock_storage_client.assert_called_once()

# Test callback endpoint
def test_validation_callback_success(api_client):
    """Test successful callback processing."""
    response = api_client.post("/api/v1/validation-callbacks/", {
        "callback_token": "...",
        "run_id": "...",
        "status": "success",
        "result_uri": "...",
    })
    assert response.status_code == 200
```

## Implementation Status

**Phase 3 (Completed):**
- ✅ envelope_builder.py - Creates typed input envelopes
- ✅ gcs_client.py - Upload/download envelopes to GCS
- ✅ job_client.py - Trigger Cloud Run Jobs via Cloud Tasks
- ✅ token_service.py - JWT tokens signed with GCP KMS
- ✅ callbacks.py - DRF endpoint for validator callbacks
- ✅ Tests for all services (7/7 passing)
- ✅ Modal.com code removed (not needed - site not yet live)

**Phase 4 (TODO):**
- Update EnergyPlusValidationEngine to use Cloud Run Jobs
- Create ValidationRun instances from workflow execution
- Integrate envelope building with validation engine
- End-to-end testing with real Cloud Run Jobs

## Alternatives Considered

### Alternative 1: Class-based service objects
```python
class EnvelopeBuilder:
    def __init__(self, run):
        self.run = run
    def build(self):
        ...
```

**Rejected:** More complex, harder to test, adds unnecessary state.

### Alternative 2: Direct Cloud Run Jobs API
```python
run_client.execute_job(job_name, env={"INPUT_URI": uri})
```

**Rejected:** No retry logic, no rate limiting, harder to monitor.

### Alternative 3: Local secret signing
```python
jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")
```

**Rejected:** Requires storing secrets, less secure than KMS.

### Alternative 4: Feature flag for Modal.com migration
```python
USE_CLOUD_RUN_JOBS = env.bool("USE_CLOUD_RUN_JOBS", default=False)
if settings.USE_CLOUD_RUN_JOBS:
    # Use Cloud Run
else:
    # Use Modal.com
```

**Rejected:** Site is not yet live, no Modal.com code exists in production. Greenfield implementation is cleaner.

## Decision

Proceed with the proposed design:
- Simple functions for envelope building, GCS, and tokens
- Cloud Tasks for job triggering
- DRF APIView for callbacks
- No feature flags (Cloud Run Jobs is the only implementation)

## Consequences

**Positive:**
- Clear, maintainable code
- Easy to test each component
- Follows Django conventions
- Simple to understand for future maintainers

**Negative:**
- More files than a single service class
- Requires GCP KMS setup for tokens
- Slightly more boilerplate than clever solutions

**Neutral:**
- Need to set up Cloud Tasks queue
- Need to configure GCS bucket lifecycle

## Next Steps

1. Create directory structure: `simplevalidations/validations/services/cloud_run/`
2. Implement each service module with tests
3. Add callback endpoint
4. Update EnergyPlusValidationEngine with feature flag
5. Write integration test
6. Document configuration
