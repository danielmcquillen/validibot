# Validation Runs

A **Validation Run** is one execution of a user Submission through a Workflow.

It records:

- Internal status and public-facing state/result (see below).
- Start and end timestamps, duration.
- Resolved configuration (rulesets, thresholds, overrides).
- A summary of results (counts of errors/warnings).
- Links to **step runs**, **findings**, and **artifacts**.

Runs provide the durable audit trail of what happened during validation.

## Status vs State vs Result

A run has three different representations of "where it is" and "what happened," each serving a different audience.

### Internal status

The `status` field on `ValidationRun` is the raw lifecycle value used by the orchestrator to manage steps:

`PENDING` → `RUNNING` → `SUCCEEDED` | `FAILED` | `CANCELED` | `TIMED_OUT`

This captures both lifecycle transitions and terminal outcomes in a single field. It's what the orchestrator writes to the database and what internal code queries against.

### Public state and result

The API and CLI expose a simpler two-field model instead of the raw status. This split is defined in `validations/constants.py` as `ValidationRunState` and `ValidationRunResult`, and the serializer maps the internal status into these fields.

**State** answers "is this done yet?" -- useful for polling loops in CI/CD:

| State | Meaning |
|-------|---------|
| `PENDING` | Not yet started |
| `RUNNING` | In progress |
| `COMPLETED` | Terminal (any outcome) |

**Result** answers "what happened?" -- useful for exit codes and automation:

| Result | Meaning |
|--------|---------|
| `PASS` | Validation succeeded, no blocking issues |
| `FAIL` | Validation ran successfully but found issues in the user's data |
| `ERROR` | The platform encountered a problem (runtime error, OOM, system failure) |
| `CANCELED` | Run was canceled |
| `TIMED_OUT` | Run exceeded its time limit |
| `UNKNOWN` | Run is not yet complete |

### Why the split?

The raw `status` field mixes two concerns: "is it still running?" and "what was the outcome?" For the orchestrator, that's fine -- it needs all the detail. But API consumers and CLI scripts need to answer those questions separately.

The critical distinction is between `FAIL` and `ERROR`:

- **FAIL** means the validator ran correctly but the user's data had problems. The `error_category` field is `VALIDATION_FAILED`.
- **ERROR** means something went wrong with the platform itself -- a container crashed, ran out of memory, or hit an infrastructure issue. The `error_category` field is `RUNTIME_ERROR`, `SYSTEM_ERROR`, `OOM`, etc.

Both map from the internal `FAILED` status, but the `error_category` field tells the serializer which public result to use. This lets a CI pipeline distinguish "your data is invalid, fix it" from "our system had a problem, try again."

### How the mapping works

The serializer in `validations/serializers.py` maps as follows:

| Internal status | error_category | → State | → Result |
|----------------|---------------|---------|----------|
| `PENDING` | -- | `PENDING` | `UNKNOWN` |
| `RUNNING` | -- | `RUNNING` | `UNKNOWN` |
| `SUCCEEDED` | -- | `COMPLETED` | `PASS` |
| `FAILED` | `VALIDATION_FAILED` | `COMPLETED` | `FAIL` |
| `FAILED` | `RUNTIME_ERROR` | `COMPLETED` | `ERROR` |
| `FAILED` | `SYSTEM_ERROR` | `COMPLETED` | `ERROR` |
| `FAILED` | `OOM` | `COMPLETED` | `ERROR` |
| `FAILED` | `TIMEOUT` | `COMPLETED` | `TIMED_OUT` |
| `CANCELED` | -- | `COMPLETED` | `CANCELED` |
| `TIMED_OUT` | -- | `COMPLETED` | `TIMED_OUT` |

### Error categories

The `error_category` field on `ValidationRun` classifies the reason for failure:

| Category | Meaning |
|----------|---------|
| `VALIDATION_FAILED` | Validator ran, found blocking issues in user's data |
| `TIMEOUT` | Hit the time limit |
| `OOM` | Out of memory |
| `RUNTIME_ERROR` | Unexpected error inside the validator |
| `SYSTEM_ERROR` | Infrastructure failure |

This field is only set when `status` is `FAILED`. It's the bridge between the internal status and the public result.

## User Field

`ValidationRun.user` records the actor that initiated a specific execution. It
is nullable because:

- API-triggered runs authenticate with organization tokens instead of Django
  users, so there is no user object to attach.
- Celery retries or scheduled replays happen asynchronously after the
  requester disconnects.
- Admins can re-run someone else's submission; in that case we capture their
  user on the new run while the submission retains the original submitter.

When the `user` column is blank the run still inherits the `submission.user`
value for auditing via `ValidationRun.submission.user`, but access checks always
fall back to the org/project relationship, not the user field.

## Nullable Project Field

`ValidationRun.project` mirrors the project captured on the submission. It is
nullable for two reasons:

1. **Optional defaults:** Workflows do not require a default project. If the UI
   or API does not supply an override, the submission/run may legitimately be
   unscoped to any project (common for organizations that treat projects as an
   advanced feature).
2. **Soft deletions:** When a project is archived we detach it from historical
   objects by setting the FK to `NULL` (see [projects](projects.md) and
   [deletions](deletions.md)). The run must remain readable even after the
   project disappears.

New code that creates runs should still pass the resolved `project_id` whenever
available so dashboards stay filterable. Rely on the project column for tenancy
but do not assume it is always populated.
