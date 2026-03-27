# Deletions

## Deleting projects

Projects use a two-phase soft-delete model. When a user deletes a project:

1. **Immediate soft-delete** -- the project is marked `is_active=False` and `deleted_at` is set to the current timestamp. All related records (workflows, validation runs, submissions, tracking events, outbound events) are **detached** by setting their project FK to `NULL`. The project row stays in the database.

2. **Deferred purge** -- the `purge_projects` management command hard-deletes soft-deleted projects older than N days. This is intended to run as a periodic task.

Default projects (one per organization) are protected and cannot be deleted.

### Why detach instead of cascade?

We treat projects and workflows as *definitions*, and validation runs as *execution history*. When a project is deleted, we want to keep all validation runs, submissions, and workflows for auditability, traceability, and support. Detaching them (setting the project FK to `NULL`) lets us purge the project row later without losing the historical records.

This means a `ValidationRun` or `Workflow` can exist with `project=None`, indicating its original project was deleted.

### Why projects have both `is_active` and `deleted_at`

Projects are the only model that tracks both fields because organisations reshuffle project boundaries frequently. We need to:

- Hide inactive projects immediately (`is_active=False`) so workflows stop accepting new runs under that namespace.
- Keep the row around until `purge_projects` removes it, so that submissions and runs that still reference the project slug in storage paths remain valid.

Workflows and workflow steps, by contrast, are versioned objects. They use `is_active` (to prevent execution), `is_locked` (to prevent edits), and now a stronger `is_tombstoned` lifecycle state for exceptional historical-record removal.

For ordinary operations:

- Draft workflows with no historical dependencies can still be deleted.
- Used workflows should normally be archived instead of deleted.
- Workflows that have issued signed credentials are protected from ordinary hard delete.

If an organization owner really must remove a credential-bearing workflow from normal product surfaces, Validibot uses a **break-glass tombstone** flow instead of deleting the row. Tombstoning:

- sets `is_tombstoned=True`
- disables launch and editing
- removes the workflow from normal lists and public/shareable surfaces
- preserves the workflow row so historical runs and signed credentials still have a stable target

That means historical workflow pages and run pages can continue to explain what happened, even after the workflow has been deliberately retired from normal use.

## Retention policy

Each workflow has configurable retention settings:

- **`data_retention`** -- how long to keep user-submitted files after validation completes. Default is `DO_NOT_STORE` (files are deleted immediately after completion; the submission record is preserved for audit).
- **`output_retention`** -- how long to keep validation outputs (results, artifacts, findings). Default is 30 days.

These are set per-workflow, not per-organisation.
