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

- **`input_retention`** -- how long to keep user-submitted content after every
  run using it reaches a terminal state.
- **`output_retention`** -- how long to keep detailed validation outputs after
  each run reaches a terminal state.

Both default to `DO_NOT_STORE`. Authors must explicitly opt in to any
post-processing retention. Input options are no retention, 1 day, 7 days, 30
days, or permanent; output options additionally include 90 days and 1 year.
The selected policies are snapshotted onto each submission/run, so a later
workflow edit cannot extend existing data's lifetime.

`DO_NOT_STORE` means deletion is queued at terminal completion. Processing
necessarily uses transient storage, and the scheduled workers normally remove
it within five minutes. Failures retry indefinitely with capped exponential
backoff and operator alerts. A purge timestamp is written only after required
external deletion succeeds.

Read access closes before physical deletion when necessary. No-retention input
labels, filenames, metadata, and bytes are hidden from result lists, detail
pages, and Django admin; finite input and output become unavailable at their
deadlines even if a storage failure is still retrying.

Input and output cleanup are independent. Input purge deletes original and
copied inputs but preserves outputs whose author-selected window is still open;
output purge later deletes the full run bundle and all detailed database
projections. Minimal identifiers, hashes, aggregate counts, status/timing, and
purge timestamps remain as the audit record. Submitter-supplied names,
filenames, arbitrary metadata, detailed errors, step values, findings,
artifacts, and evidence bytes are removed with their relevant stream.

These settings are per workflow, not per organisation. For a versioned workflow,
shortening a policy is allowed in place; extending one requires a new version.
