# ADR-001: Validation Run Execution via Cloud Tasks

**Status:** Accepted
**Date:** 2026-01-05
**Author:** Daniel McQuillen

## Context

Validation runs can take significant time to complete, especially when they include EnergyPlus or FMI simulations that run as Cloud Run Jobs. The web service needs to respond quickly to user requests without blocking on validation execution.

The current implementation has `launch()` calling `execute()` directly, which blocks the web request until all synchronous steps complete. This creates timeout risks and poor user experience.

**Behavior Change:** This ADR changes `launch()` from synchronous (sometimes returning 201 with completed results) to always-async (always returning 202 with pending status). Clients that currently rely on 201 responses with final results will need to poll for completion.

## Decision

All validation run execution will be handled asynchronously via Google Cloud Tasks. The web instance never executes validation steps directly.

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           User Request                                   │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      Web Instance (APP_ROLE=web)                         │
│                                                                          │
│  1. Validate preconditions (permissions, billing)                        │
│  2. Create ValidationRun with status=PENDING                             │
│  3. Enqueue Cloud Task with validation_run_id                            │
│  4. Return 202 Accepted immediately                                      │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ Cloud Task
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Worker Instance (APP_ROLE=worker)                     │
│                         (IAM-gated, not public)                          │
│                                                                          │
│  POST /api/v1/execute-validation-run/                                    │
│                                                                          │
│  1. Fetch ValidationRun                                                  │
│  2. Set status=RUNNING                                                   │
│  3. Execute steps sequentially:                                          │
│     - Sync validators: execute inline, continue to next                  │
│     - Async validators: launch Cloud Run Job, stop                       │
│  4. If all steps complete: set status=SUCCEEDED or FAILED                │
│  5. Return 200 OK                                                        │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ (if async validator)
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         Cloud Run Job                                    │
│                    (EnergyPlus, FMI, etc.)                               │
│                                                                          │
│  1. Execute simulation                                                   │
│  2. POST results to callback endpoint                                    │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ Direct callback
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Worker Instance (APP_ROLE=worker)                     │
│                                                                          │
│  POST /api/v1/validation-callbacks/                                      │
│                                                                          │
│  1. Record step result                                                   │
│  2. If remaining steps & passed: enqueue Cloud Task with resume_from_step│
│  3. Return 200 OK immediately                                            │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ Cloud Task (if remaining steps)
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Worker Instance (APP_ROLE=worker)                     │
│                                                                          │
│  POST /api/v1/execute-validation-run/                                    │
│  (same endpoint, with resume_from_step in payload)                       │
│                                                                          │
│  1. Execute from step N+1                                                │
│  2. Continue until complete or next async step                           │
└─────────────────────────────────────────────────────────────────────────┘
```

### Components

**Cloud Tasks Queue:**

- Production: `validibot-tasks`
- Dev/Staging: `validibot-validation-queue-{stage}`

Already exists in infrastructure. Used to deliver execution tasks to the worker. The queue name is configured via the `GCS_TASK_QUEUE_NAME` setting.

**Web Instance Changes:**

- `launch()` creates the ValidationRun and enqueues a task instead of calling `execute()`
- Always returns 202 Accepted (run is always pending when response is sent)
- New `enqueue_execution_task()` method handles task creation

**Worker Instance Changes:**

- New endpoint: `POST /api/v1/execute-validation-run/`
- Receives `validation_run_id` and `user_id` in request body
- Calls existing `execute()` method

**Task Payload:**

```json
{
  "validation_run_id": 123,
  "user_id": 456,
  "resume_from_step": null
}
```

For initial execution, `resume_from_step` is `null`. For resume after an async step callback, it contains the step order to start from:

```json
{
  "validation_run_id": 123,
  "user_id": 456,
  "resume_from_step": 4
}
```

### Idempotency

Cloud Tasks can deliver the same task more than once. The worker handles this by checking the run status before execution:

```python
def execute(self, validation_run_id, user_id, metadata, resume_from_step=None):
    validation_run = ValidationRun.objects.get(id=validation_run_id)

    # Initial execution from Cloud Tasks: only proceed if PENDING
    if resume_from_step is None:
        if validation_run.status != ValidationRunStatus.PENDING:
            return ValidationRunTaskResult(status=validation_run.status)
        validation_run.status = ValidationRunStatus.RUNNING
        validation_run.save()

    # Resume from callback: run is already RUNNING, that's expected
    elif validation_run.status != ValidationRunStatus.RUNNING:
        # If not RUNNING, something's wrong (cancelled? already finished?)
        return ValidationRunTaskResult(status=validation_run.status)

    # Proceed with execution...
```

This handles two distinct entry points:

1. **Initial execution (from Cloud Tasks):** Status must be PENDING. Transition to RUNNING atomically.
   A duplicate task delivery finds the run already RUNNING and returns early.

2. **Resume execution (from Cloud Tasks after callback):** Status is already RUNNING. No state change needed.
   The callback enqueues a new Cloud Task with `resume_from_step` to continue execution.

### Idempotent Step Execution on Retry

Cloud Tasks retries the *same* task on 5xx responses and timeouts. That means the worker can receive the same `(validation_run_id, resume_from_step)` more than once, and sometimes concurrently.

For this ADR to be correct, `execute()` must be idempotent at the step level. Concretely:

1. Create at most one `ValidationStepRun` per `(validation_run, workflow_step)` (use `get_or_create` inside `transaction.atomic()`, backed by the existing DB uniqueness constraint).
2. If the step run is already terminal (`PASSED`, `FAILED`, `SKIPPED`), skip re-execution and move to the next step.
3. If a retry re-runs a step, clear existing `ValidationFinding` rows for that step run before persisting new findings. Otherwise a retry can double-count findings.
4. Any step handler with external side effects (Slack, certificate issuance, launching a Cloud Run Job) must also be idempotent by recording an idempotency marker in `ValidationStepRun.output` and short-circuiting on retry.

Example shape (simplified):

```python
with transaction.atomic():
    step_run, created = ValidationStepRun.objects.get_or_create(
        validation_run=validation_run,
        workflow_step=wf_step,
        defaults={
            "step_order": wf_step.order,
            "status": StepStatus.RUNNING,
            "started_at": timezone.now(),
        },
    )

    if not created and step_run.status in {
        StepStatus.PASSED,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
    }:
        continue

    ValidationFinding.objects.filter(validation_step_run=step_run).delete()

```

### Error Handling

The worker returns HTTP status codes that tell Cloud Tasks whether to retry:

| Scenario | HTTP Status | Cloud Tasks Behavior |
|----------|-------------|---------------------|
| Validation failed (business logic) | 200 | No retry (task complete) |
| Run already processed (idempotent) | 200 | No retry (task complete) |
| Database connection error | 500 | Retry with backoff |
| Worker crashed mid-execution | (no response) | Retry with backoff |

For runs stuck in RUNNING due to worker crashes, the existing `cleanup-stuck-runs` scheduled task handles recovery.

### Authentication

The worker service is IAM-gated (`--no-allow-unauthenticated`). Cloud Tasks is configured with a service account that has `roles/run.invoker` permission. No application-level authentication is needed.

Callbacks from Cloud Run Jobs use the same IAM mechanism.

### Existing Idempotency Mechanisms

Two existing idempotency mechanisms remain unchanged by this ADR:

**1. Client Idempotency Keys (`@idempotent` decorator)**

Clients can send an `Idempotency-Key` header when launching validation runs. The `@idempotent` decorator on the launch endpoint caches the response and replays it for duplicate requests.

With this ADR, the cached response will always be 202 Accepted with `status: PENDING` (never 201 with final results). This is actually cleaner - the idempotency key protects against duplicate *launch* requests. Clients already need to poll or use webhooks for final results.

No changes needed to this mechanism.

**2. Callback Receipts (`CallbackReceipt` model)**

When a Cloud Run Job POSTs a callback, the `callback_id` field is used to create a `CallbackReceipt` record. If the receipt already exists, the callback returns a cached "already processed" response.

This mechanism prevents the same Cloud Run Job callback from being processed twice. It remains unchanged by this ADR.

The Phase 2 changes affect what happens *after* the callback receipt check passes (resume execution vs finalize run), but the idempotency check itself is unaffected.

### Task Configuration

```python
# Task name includes step for resume tasks to avoid dedupe collision
if resume_from_step:
    task_name = f"validation-run-{validation_run_id}-step-{resume_from_step}"
else:
    task_name = f"validation-run-{validation_run_id}"

task = {
    "http_request": {
        "http_method": "POST",
        "url": f"{worker_url}/api/v1/execute-validation-run/",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload).encode(),
        "oidc_token": {
            "service_account_email": service_account,
        },
    },
    "name": f"{queue_path}/tasks/{task_name}",
}
```

Task naming ensures:
- Duplicate initial launches are deduped (same `validation_run_id`)
- Each resume is a distinct task (step 4 resume won't collide with step 6 resume)
- A resume task won't collide with the initial task

## Consequences

### Positive

- Web requests return immediately (better UX, no timeouts)
- Clear separation between request handling and execution
- Automatic retries for transient failures
- Worker can be scaled independently
- Consistent execution model (all runs go through the same path)

### Negative

- Additional latency for simple validations (task queue overhead)
- More moving parts to monitor
- Requires worker service to be deployed and healthy

### Neutral

- Always returns 202 Accepted (no more 201 for sync completions)
- Status polling or webhooks needed for completion notification

## Future Optimization: Sync Path for Simple Workflows

In the future, we may add an optimization where workflows containing only synchronous validators (Basic, JSON, XML, AI) can be executed inline on the web instance and return 201 Created with the final result.

This would involve:

1. Analyzing the workflow to determine if all steps are synchronous
2. If yes, execute directly and return 201 with completed results
3. If no, enqueue via Cloud Tasks and return 202 as described above

This optimization is deferred because:

- It adds complexity to the launch path (two execution modes)
- The latency benefit is marginal (Cloud Tasks adds ~100-200ms)
- Consistent 202 behavior simplifies client implementation
- We can add it later without breaking API contracts (201 is a valid response)

## Design Considerations

### Callback Does Minimal Work

The callback handler's job is simple: record the step result, then enqueue a Cloud Task to resume if needed. This ensures:

1. **Cloud Run Job finishes quickly** - The job stops billing as soon as the callback returns 200
2. **No timeout risk** - Callback does ~100ms of work (record result, enqueue task), well under the 30s timeout
3. **Clean separation** - Callback handles job results; Cloud Tasks handles workflow orchestration

### Callback Must Not Finalize When Resuming

The current callback handler (`callbacks.py` lines 337-386) always sets the run to a terminal status (`run.status = SUCCEEDED/FAILED`) and sets `run.ended_at`. Phase 2 must change this:

```python
# After recording the step result...
remaining_steps = run.workflow.steps.filter(order__gt=step_run.step_order).exists()

if remaining_steps and step_run.status == StepStatus.PASSED:
    # DO NOT set run.status or run.ended_at here
    # Enqueue task to resume - it will set final status when complete
    enqueue_validation_run(
        validation_run_id=run.id,
        user_id=run.user_id,
        resume_from_step=step_run.step_order + 1,
    )
else:
    # No remaining steps or step failed - finalize now (current behavior)
    run.status = ...
    run.ended_at = ...
    run.save()

# Always return 200 OK immediately
```

The key change: only finalize the run when there are no remaining steps OR the current step failed.

### Task Name Deduplication

Cloud Tasks dedupes by task name within a 1-hour window. We use different naming patterns for initial vs resume tasks:

```python
# Initial execution
task_name = f"validation-run-{validation_run_id}"

# Resume execution (includes step to avoid collision with initial task)
task_name = f"validation-run-{validation_run_id}-step-{resume_from_step}"
```

This ensures:
- Duplicate initial launches are deduped
- Each resume is a distinct task (step 4 resume won't collide with step 6 resume)
- A resume task won't collide with the initial task

### Local Development

Docker Compose runs web (`django`) and worker containers but has no Cloud Tasks queue.

**Prerequisites for local dev:**

1. Add `APP_ROLE=worker` to the worker container environment in `docker-compose.local.yml`:

```yaml
worker:
  <<: *django
  command: ["/start-worker"]
  ports:
    - "8001:8001"
  environment:
    - APP_ROLE=worker
```

This ensures `APP_IS_WORKER=True` so the worker endpoints are available.

2. For local development, `enqueue_validation_run()` detects the local environment and calls the worker directly via HTTP:

```python
def enqueue_validation_run(
    validation_run_id: int,
    user_id: int,
    resume_from_step: int | None = None,
) -> None:
    payload = {
        "validation_run_id": validation_run_id,
        "user_id": user_id,
        "resume_from_step": resume_from_step,
    }

    if settings.DEBUG and not getattr(settings, "GCS_TASK_QUEUE_NAME", None):
        # Local dev: call worker directly using httpx (preferred over requests)
        import httpx
        httpx.post(
            "http://worker:8001/api/v1/execute-validation-run/",
            json=payload,
            timeout=300,
        )
        return

    # Production: enqueue via Cloud Tasks
    if resume_from_step:
        task_name = f"validation-run-{validation_run_id}-step-{resume_from_step}"
    else:
        task_name = f"validation-run-{validation_run_id}"
    # ... Cloud Tasks enqueue code
```

This keeps the same code path (HTTP to worker) while bypassing Cloud Tasks infrastructure locally.

## Implementation Plan

### Phase 1: Cloud Tasks Integration

1. Create `validibot/core/tasks/cloud_tasks.py` with `enqueue_validation_run()` function
2. Add `ExecuteValidationRunView` to worker API
3. Register new endpoint in `api_internal_router.py`
4. Modify `ValidationRunService.launch()` to enqueue instead of execute
5. Update `ValidationRunService` docstrings to reflect new architecture

### Phase 2: Callback Resume Logic

The current callback handler (`ValidationCallbackView`) finalizes the entire run when an
async step completes. This is incorrect for workflows with steps after the async validator.

6. Add `resume_from_step` parameter to `execute()`:

```python
def execute(
    self,
    validation_run_id: int,
    user_id: int,
    metadata: dict | None = None,
    resume_from_step: int | None = None,  # step order to start from
) -> ValidationRunTaskResult:
    # ...
    workflow_steps = workflow.steps.all().order_by("order")
    if resume_from_step is not None:
        workflow_steps = workflow_steps.filter(order__gte=resume_from_step)

    for wf_step in workflow_steps:
        # ... existing step execution logic
```

This is cleaner than inferring state from the database - the caller explicitly tells
`execute()` where to resume from.

7. Update `ExecuteValidationRunView` to accept `resume_from_step`:

```python
class ExecuteValidationRunView(APIView):
    def post(self, request):
        validation_run_id = request.data["validation_run_id"]
        user_id = request.data["user_id"]
        resume_from_step = request.data.get("resume_from_step")  # None or int

        service = ValidationRunService()
        service.execute(
            validation_run_id=validation_run_id,
            user_id=user_id,
            resume_from_step=resume_from_step,
        )
        return Response({"status": "ok"})
```

8. Refactor `ValidationCallbackView.post()` to enqueue resume task:

```python
# After recording the async step result...
# (The callback handler already resolves step_run from the RUNNING step)

remaining_steps = run.workflow.steps.filter(
    order__gt=step_run.step_order
).exists()

if remaining_steps and step_run.status == StepStatus.PASSED:
    # Enqueue task to resume from next step
    enqueue_validation_run(
        validation_run_id=run.id,
        user_id=run.user_id,
        resume_from_step=step_run.step_order + 1,
    )
    # DO NOT set run.status or run.ended_at - the resume task will finalize
else:
    # Finalize the run (current behavior)
    run.status = ...
    run.ended_at = ...
    run.save()

# Always return 200 OK immediately
```

The callback does minimal work: record the result, enqueue if needed, return 200.
The Cloud Run Job finishes quickly and stops billing.

### Phase 3: Testing

9. Add integration tests for the full flow (including multi-step with async)
10. Update existing unit tests that expect synchronous execution
11. Add specific test for callback-resume scenario

## References

- [Google Cloud Tasks documentation](https://cloud.google.com/tasks/docs)
- [Cloud Run IAM authentication](https://cloud.google.com/run/docs/authenticating/service-to-service)
- Existing worker deployment: `just gcp-deploy-worker`
- Existing queue: `validibot-tasks` (prod) / `validibot-validation-queue-{stage}` (dev/staging)
