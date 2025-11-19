# Projects and Validation Context

Projects provide an optional namespace inside each organization so teams can
separate API keys, usage, and reporting without creating entirely new orgs.
They act as a contextual tag that flows from content definitions
(`Workflow`) into execution history (`Submission` and `ValidationRun`).

## Why Projects Exist

- **Org-level segmentation:** Many customers operate multiple programs or
  departments under one organization. Projects let them keep the same member
  roster but report usage separately.
- **Defaults for automation:** Workflows, validators, and integrations often
  need to know which downstream system to bill or notify. A default project
  removes guesswork.
- **Access control clues:** Dashboards and API responses can quickly filter by
  project ID without re-joining workflows or submissions, which keeps our
  multi-tenant boundaries simple (see
  `docs/dev_docs/overview/platform_overview.md`).

Every organization has one default project (`Project.is_default`). Additional
projects are soft-deleted rather than hard-deleted so historical runs remain
auditable. See `docs/dev_docs/data-model/deletions.md` for the lifecycle.

## Workflow → Submission → ValidationRun

The project foreign key appears in all three layers intentionally:

| Layer | Why Project Is Stored |
| ----- | --------------------- |
| `Workflow` | Records the *recommended* project for future runs. This is editable so teams can reorganize without cloning the workflow. |
| `Submission` | Captures the project that was resolved at launch time. Submissions may override the workflow default through query params or UI selection, so the resolved value is not always equal to `workflow.project`. |
| `ValidationRun` | Copies the submission’s project for immutable history. Runs often outlive the submission content and need to be filterable without joins. |

The `ValidationRun` docstring summarizes this denormalization
(`simplevalidations/validations/models.py:1105-1112`). Copying the project to
both `Submission` and `ValidationRun` is what lets us:

1. **Allow overrides:** `WorkflowViewSet.start_validation` accepts a
   `project_slug` query parameter (see
   `docs/dev_docs/overview/request_modes.md:124-129`). Without storing the
   resolved project on the submission/run we would lose the caller’s intent as
   soon as someone reassigns the workflow.
2. **Keep history accurate:** Workflows are frequently moved between projects
   (`docs/dev_docs/organization_management.md:41-53`). If history rows only
   referenced `workflow.project` our historical dashboards would show a
   different project after every reassignment.
3. **Simplify access control:** Many queries scope by `org_id` and `project_id`
   simultaneously. Indexes on those fields in `Submission` and `ValidationRun`
   keep dashboards snappy and enforce tenant boundaries without extra joins.
4. **Partition storage:** Upload paths incorporate `submission.project.slug`
   (`simplevalidations/submissions/models.py:32-55`). The FK is part of how we
   spread files across buckets/prefixes.

## Project Resolution Flow

The request pipeline resolves the project before we touch serializers:

1. The workflow or launch UI determines the base project (usually
   `workflow.project`). Leaving it blank is allowed for orgs that have not
   adopted projects yet.
2. Callers may supply an override (query string or form input). We ensure
   the override belongs to the same org.
3. `_process_structured_payload` attaches the resolved project to the
   `Submission` and mirrors it into the new `ValidationRun`.

This mirrors our working agreement to "keep workflow, validation, and
submission objects aligned on org/project/user" (platform overview section).

## Deletion and Reassignment

When a project is soft-deleted we detach it from workflows, submissions, runs,
tracking events, and outbound events by setting the FK to `NULL`. This is why
`Submission.project` uses `CASCADE` (historical records belong to the org) while
`ValidationRun.project` uses `SET_NULL` (runs must survive even if the project
disappears). The detachment keeps historical audit trails intact while making
space for the project slug to be recycled later. See
`docs/dev_docs/data-model/deletions.md` for the sequence.

When workflows move between projects the data flow is:

1. Editor updates `workflow.project` via the workflow settings form.
2. Existing submissions/runs keep their stored `project_id`.
3. New submissions inherit the new default unless the caller overrides it.

## Implementation Guidelines

- Always pass explicit `project_id` values when creating submissions and runs,
  even if you believe the workflow default matches. This prevents accidental
  drift during refactors.
- Prefer querying submission/run tables directly for reporting. They have
  indexes on `(org, project, workflow, created)` specifically to avoid
  cross-table joins (`simplevalidations/submissions/models.py:64-80` and
  `simplevalidations/validations/models.py:1115-1119`).
- When writing migrations or cleanup jobs, detach project references by setting
  them to `NULL` rather than trying to infer a new project.
- If integrations in `../sv_modal` or `../sv_shared` need project context,
  fetch it from the submission/run instance passed into the engine rather than
  re-querying the workflow.

Keeping the project FK on all three tables is therefore not duplication but a
tenant-safety requirement that protects overrides, historical truth, storage
layout, and query performance.
