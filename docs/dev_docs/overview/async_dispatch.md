# Async Dispatch Architecture

Validibot runs on three different deployment targets — tests, Docker
Compose, and GCP Cloud Run. Each has a different mechanism for
moving work out of a synchronous request: tests run inline, Docker
Compose uses Celery with a Redis broker, GCP uses Cloud Tasks
calling a worker-only HTTP endpoint. Rather than scattering
`DEPLOYMENT_TARGET == "gcp"` branches throughout the codebase, we
use a **dispatcher** abstraction: callers describe *what* they want
to happen, and a registry picks the right *how* for the current
deployment.

Two dispatcher hierarchies exist today:

| Domain | Package | Used for |
|---|---|---|
| Validation runs | `validibot/core/tasks/dispatch/` | Launching a validation workflow execution |
| Tracking events | `validibot/tracking/dispatch/` | Recording login/logout + app-event rows out of band |

They share no base class. Their payloads differ, and forcing a
common ancestor would mean polluting both with fields the other
doesn't need. What they *do* share is the pattern — same ABC shape,
same registry + `lru_cache` factory, same "`dispatch()` must not
raise for predictable failures" contract. Reading one teaches you
the other.

## Why a dispatcher, not just `if DEPLOYMENT_TARGET == ...`

Before the tracking refactor, `validibot.tracking.signals`
unconditionally called `log_tracking_event_task.delay()`. On GCP
there is no Redis broker — Celery tried to connect to
`localhost:6379`, refused the connection, and the whole 2FA login
request returned 500. Two lessons came out of that:

1. **Transport choice is a deployment concern.** Application code
   should describe the intent ("record this event") and leave the
   mechanism (Celery, Cloud Tasks, inline) to a layer that knows the
   target.
2. **The auth-path critical section must never die because a queue
   is unavailable.** The dispatcher contract enforces this: all
   transport failures must return a response with `error` set, not
   raise.

## Anatomy of a dispatcher package

Both packages have the same four-file shape:

```
<domain>/dispatch/
├── __init__.py           re-exports the public names
├── base.py               ABC + request/response dataclasses
├── registry.py           factory: picks one dispatcher per DEPLOYMENT_TARGET
├── <target>.py           one file per backend (cloud_tasks, celery, inline, …)
```

### `base.py` — contract

- `*Request` dataclass — **primitives only** (PKs, not ORM
  instances). Task-queue serialisers require JSON, and resolving a
  stale ORM instance from the signal-thread transaction could race
  with the worker-side read.
- `*DispatchResponse` dataclass — `task_id` (full Cloud Tasks
  resource name / Celery UUID / `None` for inline), `is_sync`,
  optional `error`.
- `*Dispatcher` ABC — `dispatcher_name`, `is_sync`, `is_available()`,
  `dispatch(request)`.

The core rule, repeated in every base.py: **`dispatch()` must not
raise for transient or predictable failures**. Populate
`response.error` instead.

### `registry.py` — deployment-target routing

```python
@lru_cache(maxsize=1)
def get_tracking_dispatcher() -> TrackingDispatcher:
    target = getattr(settings, "DEPLOYMENT_TARGET", "test")
    ...
```

`lru_cache` means the dispatcher is built once per process. Clear
with `clear_tracking_dispatcher_cache()` in tests that need to swap
the target. The validation-run registry works identically.

### One backend file per target

- **Inline / test** — calls the service directly; `is_sync=True`.
  Used by pytest and by a developer running `just local up` without
  Celery.
- **Celery** — wraps `task.delay()` with a broad `try/except` that
  catches broker failures and logs. `is_sync=False`.
- **Cloud Tasks** — builds a `tasks_v2.Task` with an OIDC token
  addressed to a worker-only HTTP endpoint. `is_sync=False`. The
  worker receives the task, verifies the OIDC token via
  `CloudTasksOIDCAuthentication` (signature + audience + SA
  allowlist), and calls the same service method inline.

## Worker-side endpoints

Every Cloud Tasks dispatcher has a matching worker endpoint. They
all sit behind `WorkerOnlyAPIView`, which:

1. Returns 404 from any non-worker instance (`APP_IS_WORKER=False`),
   so a public probe that guesses the URL learns nothing.
2. Delegates authentication to
   `get_worker_auth_classes()`, which selects Cloud Run IAM + OIDC on
   `gcp` and the shared-secret `WORKER_API_KEY` header on
   `docker_compose`.

Current Cloud Tasks targets:

| URL | Dispatcher | View |
|---|---|---|
| `POST /api/v1/execute-validation-run/` | `GoogleCloudTasksDispatcher` | `ExecuteValidationRunView` |
| `POST /api/v1/tasks/tracking/log-event/` | `CloudTasksTrackingDispatcher` | `LogTrackingEventView` |
| `POST /api/v1/scheduled/*` | Cloud Scheduler (not a dispatcher, but same auth pattern) | `SendPeriodicEmailsView`, etc. |

**URL path as module constant.** The tracking dispatcher exports
`WORKER_ENDPOINT_PATH = "/api/v1/tasks/tracking/log-event/"` at
module level. The router imports the same constant. If the path
drifts, the build fails rather than one side silently posting to a
404.

## `transaction.on_commit` is the caller's job

The dispatcher layer doesn't wrap calls in
`transaction.on_commit` — that's caller context. Signal receivers
and service methods that enqueue work inside a DB transaction call
`transaction.on_commit(lambda: dispatcher.dispatch(req))` themselves.
This keeps dispatchers transport-only and leaves the transaction
question with whoever actually has the transaction.

See `validibot/tracking/signals.py:_enqueue_tracking_event` for the
canonical example, including the **last-resort `try/except`** around
the dispatcher call. The dispatcher contract promises no transient
exception escapes, but a genuine programming error (bad import,
attribute error) could still surface. Auth must not 500 because of
a tracking bug — the safety net logs with `exc_info=True` and
returns.

## Adding a new backend

Concretely, adding AWS SQS support for the tracking domain would
mean:

1. New file: `validibot/tracking/dispatch/sqs.py` with
   `SQSTrackingDispatcher(TrackingDispatcher)`.
2. New branch in `registry.py` when `DEPLOYMENT_TARGET == "aws"`.
3. New worker endpoint if the queue needs one (or a worker process
   polling the queue, in which case no endpoint).
4. Tests in `validibot/tracking/tests/test_dispatch.py` covering
   registry selection, success, config-missing, client-error.

Signal receivers and service callers change nothing.

## Extending to a new domain

If you have a new class of work that needs the same
target-agnostic routing:

1. Create `<domain>/dispatch/` with the four-file shape.
2. Mirror the ABC / dataclasses / registry from one of the existing
   packages — they're deliberately parallel.
3. Add a worker endpoint under `WorkerOnlyAPIView` if Cloud Tasks is
   in the target list.
4. Update this document's registry table.

Resist the temptation to share a base class across domains. The
cost (coupling two request shapes) outweighs the benefit (one fewer
ABC file).

## Related docs

- [Service Architecture](service_architecture.md) — in-process
  service layer that dispatchers hand off to.
- [Execution Backends](execution_backends.md) — how validator
  containers actually run on each deployment target; sits one layer
  below the dispatcher.
