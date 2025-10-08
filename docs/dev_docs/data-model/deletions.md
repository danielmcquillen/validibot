# Deletions in SimpleValidations

## Deleting Projects

Users can delete a project, upon which the project is set to inactive and a deletion date is set.
Default projects created for each organization are protected and cannot be deleted.

Then SimpleValidations has a periodic task or management command that deletes all
projects older than a time period N.

When a project is first deleted and made "inactive" all workflow and workflow steps
stop accepting new runs, but existing execution history remains readable. The management
command `purge_projects` hard-deletes soft-deleted projects older than N days.

When a project is soft-deleted, the workflow and workflow steps linked to it are archived
along with it, but any related Validation and ValidationStep results are not deleted—they are
detached by setting the project foreign key to `NULL`.

We treat Project / Workflow / WorkflowStep as definitions, and Validation / ValidationStep as immutable execution history. When a project (or workflow) is deleted, we keep all Validations and ValidationSteps for auditability, traceability, and support—just detach them from the deleted definitions.

## Why do we keep execution history?

- Audit & compliance: we want to prove "what rules ran on which inputs" even if teams reorganise or delete a project.
- Support: Past failures/successes help debug user issues.
- Analytics: Long-term stats (pass rates, drift) depend on historical runs.

## Retention policy (make it explicit)

Each org has a configurable policy:

- Default: keep runs forever (cheap if artifacts are in object storage).
- Optional: auto-expire after N days (e.g., 365/730) unless placed on legal hold.
- Artifacts vs. metadata: you might expire large blobs first, keep lightweight metadata longer.
