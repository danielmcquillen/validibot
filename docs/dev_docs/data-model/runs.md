# Validation Runs (Jobs)

A **Validation Run** (sometimes just called a "Job") is one execution of a Submission through a workflow.

It records:

- Status (`PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELED`, `TIMED_OUT`).
- Derived state (`PENDING`, `RUNNING`, `COMPLETED`) and result (`PASS`, `FAIL`, `ERROR`, `CANCELED`, `TIMED_OUT`, `UNKNOWN`) for API/CLI consumers.
- Start and end timestamps.
- Duration.
- Resolved configuration (rulesets, thresholds, overrides).
- A summary of results (e.g. counts of errors/warnings).
- Links to **step runs**, **findings**, and **artifacts**.

Runs provide the durable audit trail of what happened during validation.

## User Field

`ValidationRun.user` records the actor that initiated a specific execution. It
is nullable because:

- API-triggered runs authenticate with organization tokens instead of Django
  users, so there is no user object to attach.
- Celery retries or scheduled replays happen asynchronously after the
  requester disconnects.
- Admins can re-run someone else’s submission; in that case we capture their
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
   objects by setting the FK to `NULL` (see `data-model/projects.md` and
   `data-model/deletions.md`). The run must remain readable even after the
   project disappears.

New code that creates runs should still pass the resolved `project_id` whenever
available so dashboards stay filterable. Rely on the project column for tenancy
but do not assume it is always populated.
