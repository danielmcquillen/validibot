# Deletions in Validibot

## Deleting Projects

Users can delete a project, upon which the project is set to inactive and a deletion date is set.
Default projects created for each organization are protected and cannot be deleted.

Then Validibot has a periodic task or management command that deletes all
projects older than a time period N.

When a project is first deleted and made "inactive" all workflow and workflow steps
stop accepting new runs, but existing execution history remains readable. The management
command `purge_projects` hard-deletes soft-deleted projects older than N days.

When a project is soft-deleted, the workflow and workflow steps linked to it are archived
along with it, but any related Validation and ValidationStep results are not deleted—they are
detached by setting the project foreign key to `NULL`.

We treat Project / Workflow / WorkflowStep as definitions, and Validation / ValidationStep as immutable execution history. When a project (or workflow) is deleted, we keep all Validations and ValidationSteps for auditability, traceability, and support—just detach them from the deleted definitions.

### Why Projects Track `deleted_at`

Projects are the only definition model with both `is_active` **and** `deleted_at`
(`validibot/projects/models.py:92-186`) because organizations reshuffle
project boundaries frequently. We need to:

- Hide inactive projects immediately (`is_active=False`) so workflows stop
  accepting new runs under that namespace.
- Keep the row around until the `purge_projects` command removes it, ensuring
  auditability for submissions/runs that still reference the project slug in
  storage paths.

Workflows and workflow steps, by contrast, are already versioned objects. They
use an `is_active` flag (`validibot/workflows/models.py:92-205`) plus
`is_locked` to prevent edits but are never physically deleted; version history
is part of the product. Validators follow the same pattern. If we ever need to
retire a workflow entirely we mark it inactive (so it cannot execute) and rely
on the parent project deletion flow to eventually detach or purge it. This keeps
the soft-delete complexity limited to the project boundary where cross-tenant
references live.

## Why do we keep execution history?

- Audit & compliance: we want to prove "what rules ran on which inputs" even if teams reorganise or delete a project.
- Support: Past failures/successes help debug user issues.
- Analytics: Long-term stats (pass rates, drift) depend on historical runs.

## Retention policy (make it explicit)

Each org has a configurable policy:

- Default: keep runs forever (cheap if artifacts are in object storage).
- Optional: auto-expire after N days (e.g., 365/730) unless placed on legal hold.
- Artifacts vs. metadata: you might expire large blobs first, keep lightweight metadata longer.
