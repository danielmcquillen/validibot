# ADR-2026-01-06: SaaS Reliability Hardening for Worker Internals

**Status:** Proposed
**Owners:** Platform / Validations
**Related ADRs:**
- `docs/adr/2026-01-05-validation-run-execution-via-cloud-tasks.md`
- `docs/adr/2025-01-05-django-6-tasks-evaluation.md`
- `docs/adr/completed/2025-12-09-callback-idempotency.md`
- `docs/adr/completed/2025-11-27-idempotency-keys.md`
**Related code:**
- `validibot/core/tasks/cloud_tasks.py`
- `validibot/validations/api/execute.py`
- `validibot/validations/api/callbacks.py`
- `validibot/validations/services/validation_run.py`
- `validibot/validations/services/validation_callback.py`

---

## Context

Validibot’s execution path is intentionally asynchronous:

- The web service enqueues a Cloud Task (`execute-validation-run`).
- The worker service executes steps and may launch Cloud Run Jobs.
- Async validators POST back to the worker via `validation-callbacks`.

This architecture gives us good user-facing latency and a clean separation between “web” and “worker”, but it also means reliability issues show up quickly:

- Infrastructure calls retry automatically (Cloud Tasks / Cloud Run Jobs).
- A small amount of accidental retry amplification can create a lot of load.
- A single “stuck” run can confuse users and erode trust.

This ADR proposes three hardening improvements:

1. Callback idempotency without holding long DB locks during network I/O
2. Clear error classification to avoid retry storms
3. Better observability and automated reconciliation for “stuck” runs

## Goals

- Keep worker endpoints stable under retries and partial failures.
- Avoid duplicate work and minimize unnecessary retries.
- Make it easy to debug a single run end-to-end from logs alone.
- Prefer straightforward Django patterns over clever concurrency tricks.

## Non-goals

- Redesigning the overall Cloud Tasks + Cloud Run architecture.
- Making worker endpoints public or adding end-user authentication to them.
- Replacing the current callback model with a full message bus.

---

## Considered Alternatives

### Django 6.0 Tasks framework

Django 6 adds a built-in Tasks framework (`django.tasks`) that standardizes task definition, argument validation,
queuing, and result tracking. Importantly, it does **not** include a production worker: execution still needs a
separate worker process/service and a production-ready task backend.

We considered using Django Tasks for this ADR because it has a nice developer experience (`@task`, `enqueue()`,
`TaskResult`, `ImmediateBackend` for tests) and it surfaces useful concepts like attempt counts and task claiming.

We are **not** adopting it for this ADR because our reliability problems are primarily at the HTTP boundaries
(Cloud Tasks → worker execute endpoint, Cloud Run Job → worker callback endpoint), and the core work happens in
external containers that report completion via callbacks. That callback-based, external execution model doesn’t map
cleanly to “a Python function runs in a Django worker and returns a value”, and we’d still need to choose/build a
production backend (likely on top of Cloud Tasks), duplicating queueing infrastructure we already operate.

If we ever revisit this, Django Tasks also imposes some constraints we already need to respect in our Cloud Tasks
pipeline:

- Task arguments and return values must be JSON-serializable.
- When enqueuing work that depends on database writes, enqueue on `transaction.on_commit()` so workers don’t race
  ahead of the commit.

Where Django Tasks may still be a good fit later:

- Small internal background jobs that run inside Django (email, cleanup, rollups) where a task return value is useful.
- Local development/testing, where `ImmediateBackend` can run tasks inline without extra infrastructure.

Decision: do not use Django Tasks for validator execution or callback processing as part of this ADR. Revisit after a
Django 6 upgrade if a mature backend fits our GCP deployment model.

Reference: https://docs.djangoproject.com/en/6.0/topics/tasks/

---

## Decision

### 1) Lease-based callback receipt handling (short transactions)

Today, callback idempotency is guarded by holding a database row lock while the callback is processed. This can block other callbacks (or retries) while we do network I/O such as downloading envelopes from GCS.

We will switch to a **lease** model:

- A callback with a `callback_id` “claims” processing for a short period.
- Processing happens outside the claim transaction.
- If the process crashes, the lease expires and the next retry can take over.

#### Proposed schema change

Add a small set of fields to `CallbackReceipt` to support leasing:

- `lease_expires_at` (datetime, indexed)
- `last_error` (text, optional)
- `attempt_count` (integer, optional)

We keep `status` as:

- `PROCESSING` while the lease is held
- final callback status (e.g. `"success"`, `"failed_runtime"`) when done

#### Proposed algorithm (happy path)

- If receipt does not exist: create it with `status=PROCESSING` and `lease_expires_at=now+lease_window`.
- If receipt exists and `status != PROCESSING`: return 200 (already processed).
- If receipt exists and `status == PROCESSING`:
  - If `lease_expires_at > now`: return 409 (another worker is processing; retry later).
  - If `lease_expires_at <= now`: take over by updating the lease and proceed.

This reduces lock time to a small “claim” update, rather than holding a lock through the whole callback.

### 2) Explicit error classification for worker endpoints

Worker endpoints are called by infrastructure that retries on non-2xx responses. Returning “500 for everything” is simple, but it can cause retries for errors that will never succeed.

We will add an explicit classification of failures into:

- **Retryable errors** (return non-2xx so infra retries)
  - GCS transient failures when downloading envelopes
  - Database connectivity issues
  - Lease contention (409 conflict)
- **Non-retryable errors** (return 2xx so infra stops retrying)
  - Invalid payload schema (missing fields, wrong types)
  - Envelope identity mismatches (wrong run, wrong org, wrong validator)
  - Run not found / run not in a resumable state

When a non-retryable error occurs, we should still do two things:

1. Log clearly with structured context (run_id, callback_id, etc.)
2. Mark the run terminal with a useful `error_category` so the UI has an explanation

### 3) Correlation IDs + reconciliation improvements

We will standardize worker-side logging so that a run can be traced end-to-end:

- Include `run_id`, `step_run_id`, `callback_id`, `resume_from_step`, and Cloud Tasks task name (when available) as consistent log keys.
- Capture and log Cloud Run trace context headers when present (`X-Cloud-Trace-Context`).

We will also improve reconciliation for stuck runs:

- Extend `cleanup_stuck_runs` to distinguish:
  - “stuck because callback never arrived” vs
  - “stuck because resume task didn’t run”
- For resumable cases, prefer re-enqueueing before timing out.

---

## Consequences

- Requires a small migration for `CallbackReceipt` fields.
- Adds a small amount of code complexity, but concentrates it in one service (`ValidationCallbackService`) instead of spreading it across views.
- Makes retry behavior more predictable and reduces the chance of retry storms.

---

## Effort Estimate

If we keep the scope tight and focus on the worker internals only:

- Lease-based callback receipts + tests + migration: ~1–2 days
- Error classification changes + tests: ~0.5–1 day
- Observability (correlation IDs + improved stuck-run reconciliation): ~1–2 days

Total: ~3–5 days of focused work, plus time for end-to-end testing in a real GCP environment.

---

## Implementation Notes (Suggested Work Items)

- Add leasing fields + migration for `CallbackReceipt`
- Update `ValidationCallbackService` to claim/lease and process outside of the claim transaction
- Add explicit exception handling for payload validation, envelope mismatch, and “run not found”
- Add consistent structured logging helpers used by:
  - `enqueue_validation_run` (`validibot/core/tasks/cloud_tasks.py`)
  - `ExecuteValidationRunView` (`validibot/validations/api/execute.py`)
  - `ValidationCallbackService` (`validibot/validations/services/validation_callback.py`)
- Update / add tests for:
  - lease takeover after expiration
  - non-retryable error responses and run terminalization
  - correlation keys present in logs (smoke-level assertions)
