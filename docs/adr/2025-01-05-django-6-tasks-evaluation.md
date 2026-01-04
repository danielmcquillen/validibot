# ADR: Django 6 Tasks Framework Evaluation for Local Development

**Date:** 2025-01-05
**Status:** Proposed
**Context:** Evaluate whether Django 6's new Tasks framework can simplify local development of async validation workflows while preserving the production Cloud Run Jobs architecture.

## Summary

After careful analysis, we conclude that Django 6 Tasks is **not a good fit** for our validator execution architecture. The framework is designed for work that Django performs itself, not for triggering external containers with callback-based completion. However, upgrading to Django 6 is still worthwhile for other reasons, and we can implement a simpler local development mode without the Tasks framework.

## Problem Statement

Running async validation workflows locally is difficult because:

1. **Cloud Run Jobs don't exist locally** - We trigger jobs via the GCP Jobs API
2. **Callbacks can't reach localhost** - Jobs POST results back, but can't reach local machines
3. **Validators require specialized binaries** - EnergyPlus, FMU runtimes aren't on dev machines
4. **GCS doesn't exist locally** - Input/output envelopes are stored in Cloud Storage

The hope was that Django 6 Tasks could provide an abstraction layer that works differently in local vs. production environments.

## Current Production Architecture

```
Django Web                    Cloud Run Job Container              Django Worker
─────────────                 ─────────────────────────            ─────────────
1. Create ValidationRun
2. Upload envelope to GCS
3. Trigger Cloud Run Job ────► 4. Download envelope from GCS
   (via Jobs API)                 5. Run EnergyPlus binary
                                  6. Upload output to GCS
                                  7. POST callback ──────────────► 8. Download output
                                     (with GCP ID token)              9. Update ValidationRun
                                                                      10. Create findings
```

Key characteristics:
- **External execution**: Validation runs in a separate container with specialized binaries
- **Callback-based completion**: Jobs POST results back; Django doesn't poll for completion
- **GCS-mediated data**: Input/output envelopes flow through Cloud Storage
- **IAM authentication**: Callbacks authenticated via Cloud Run IAM, not Django tokens

## Django 6 Tasks Framework Analysis

### What Django 6 Tasks Provides

```python
from django.tasks import task

@task
def send_email(to: str, subject: str, body: str):
    # This code runs in a worker process
    mail.send(to, subject, body)
    return {"sent": True}

# Enqueue the task
result = send_email.enqueue(to="user@example.com", ...)
```

Backends:
- `ImmediateBackend`: Runs task synchronously in the web process (dev)
- `DummyBackend`: Stores tasks without executing (testing)
- Custom backends: Redis, Celery, etc. (production)

### Why It Doesn't Fit Our Architecture

#### 1. External Container Execution

Django Tasks assumes the task function runs inside a Django worker process:

```python
@task
def run_energyplus_validation(run_id: int):
    # This would need to actually run EnergyPlus...
    # But we don't have EnergyPlus installed in Django!
    subprocess.run(["energyplus", ...])  # ❌ Not available
```

Our validators run in specialized Docker containers with:
- EnergyPlus 24.2 (400MB+ binary)
- FMU runtime libraries
- Specific Python dependencies

These can't be installed in the Django container without massively bloating it.

#### 2. Callback-Based vs. Return-Based

Django Tasks expects tasks to return results:

```python
@task
def process_data(data: dict) -> dict:
    result = heavy_computation(data)
    return result  # Framework captures this

result = process_data.enqueue(data)
value = result.return_value  # Available after completion
```

Our architecture uses callbacks:

```python
# Cloud Run Job (external container)
def main():
    # ... run validation ...
    post_callback(
        callback_url="https://worker.example.com/api/v1/validation-callbacks/",
        run_id=run_id,
        result_uri="gs://bucket/output.json",
    )
    # Job exits; no return value to Django
```

There's no natural way to bridge these models. We could:
- Have the task function poll for completion (defeats the purpose of async)
- Create a custom backend that "returns" after receiving a callback (complex, fragile)

#### 3. Arguments Must Be JSON-Serializable

Django Tasks requires JSON-serializable arguments. Our current approach passes model instances:

```python
def launch_energyplus_validation(
    run: ValidationRun,  # Django model instance
    validator: Validator,
    submission: Submission,
    ...
):
```

We'd need to refactor to pass IDs and re-fetch inside the task:

```python
@task
def run_validation(run_id: int, validator_id: int, ...):
    run = ValidationRun.objects.get(id=run_id)
    # Re-fetch everything...
```

This is solvable but adds complexity.

#### 4. No Cloud Run Jobs Backend

There's no Django Tasks backend for Cloud Run Jobs. We'd need to write one, and it would need to:
- Trigger the job via Jobs API
- Somehow handle the async callback completion
- Map callback reception to task result

This is a significant custom development effort for an awkward fit.

## Alternative: Simple Local Development Mode

Instead of Django 6 Tasks, implement a targeted local development mode:

### Option A: Mock Validator Mode

Add a `MOCK_VALIDATORS=True` setting that:

1. Skips Cloud Run Job launch entirely
2. Immediately generates mock validation results
3. Updates ValidationRun as if a callback was received
4. Allows testing the full UI flow without real validation

```python
# In launcher.py
if settings.MOCK_VALIDATORS:
    return mock_validation_result(run, validator)
else:
    return launch_cloud_run_job(...)
```

**Pros**: Simple, fast, no external dependencies
**Cons**: Doesn't test real validation logic

### Option B: Local Container Mode

1. Run validator containers locally via Docker Compose
2. Use filesystem instead of GCS for envelope storage
3. Set callback URL to localhost
4. Validators POST to local Django worker

```yaml
# docker-compose.local.yml
services:
  web:
    ...
  worker:
    ...
    ports:
      - "8001:8000"
  validator-energyplus:
    image: validibot-validator-energyplus
    environment:
      - CALLBACK_HOST=http://host.docker.internal:8001
```

**Pros**: Tests real validation logic, closer to production
**Cons**: Requires running validator containers, more setup

### Option C: Tunnel to GCP Dev

1. Use ngrok or similar to expose localhost
2. Set `WORKER_URL` to the tunnel URL
3. Trigger real Cloud Run Jobs on GCP dev
4. Jobs call back to tunnel → localhost

**Pros**: Uses actual GCP infrastructure
**Cons**: Requires internet, tunnel setup, GCP costs

## Recommendation

1. **Do not adopt Django 6 Tasks for validator execution** - The framework doesn't fit our callback-based, external-container architecture.

2. **Upgrade to Django 6 for other benefits** - New features, security updates, Python 3.12+ support. Just don't use the Tasks framework for validators.

3. **Implement Option A (Mock Validator Mode) for routine development** - Quick, simple, tests the Django orchestration layer.

4. **Use Option B (Local Container Mode) for integration testing** - When you need to verify actual validation logic.

5. **Use Option C (Tunnel) for debugging production issues** - When you need to step through Django code while using real GCP infrastructure.

## Configuration

```python
# settings/local.py

# Mock mode: skip real validation, return fake results instantly
MOCK_VALIDATORS = env.bool("MOCK_VALIDATORS", default=True)

# Local container mode: use filesystem instead of GCS
GCS_VALIDATION_BUCKET = ""  # Empty = use local filesystem
WORKER_URL = "http://localhost:8001"  # Local worker process
```

## Implementation Plan

### Phase 1: Mock Validator Mode
1. Add `MOCK_VALIDATORS` setting
2. Create `mock_validation_result()` function in launcher.py
3. Generate realistic-looking mock results (some pass, some fail)
4. Document in developer setup guide

### Phase 2: Local Container Mode (Optional)
1. Add validator containers to docker-compose.local.yml
2. Update launcher to use filesystem when GCS bucket not set
3. Configure callback URLs for local network
4. Document setup and usage

### Phase 3: Django 6 Upgrade (Separate)
1. Upgrade Django 5.2 → 6.0
2. Update deprecated APIs
3. Update dependencies (Python 3.12+ already satisfied)
4. Do not implement Tasks framework for validators

## Decisions Made

1. **Django 6 Tasks is not suitable** for our validator execution model due to:
   - External container execution (not Django workers)
   - Callback-based completion (not return-based)
   - No Cloud Run Jobs backend available

2. **Mock Validator Mode is the primary local solution** for day-to-day development.

3. **Local Container Mode is optional** for integration testing scenarios.

4. **Django 6 upgrade proceeds independently** of the Tasks framework decision.

## Appendix: What Django 6 Tasks IS Good For

If we later need background tasks that Django itself performs, the Tasks framework would be excellent:

- Sending emails (already sync, but could be backgrounded)
- Generating reports from database queries
- Processing uploaded files (thumbnails, conversions)
- Cleanup jobs (expiring old records)

These are all cases where the work happens inside Django, not in external containers.
