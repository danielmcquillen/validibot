# ADR-2026-01-06: Org-scoped API Routing and Versioned Workflow Identifiers

**Status:** Proposed
**Owners:** Platform / API
**Related ADRs:**
- `docs/adr/2025-12-22-cli-api-support.md`
- `docs/adr/archive/2025-11-28-public-workflow-access.md`
- `docs/adr/2026-01-06-org-scoped-web-urls.md` (companion)
**Related code:**
- `config/urls_worker.py`
- `config/api_router.py`
- `validibot/users/scoping.py`
- `validibot/workflows/urls.py`
- `validibot/workflows/models.py`
- `validibot/workflows/views.py` (`WorkflowViewSet`)

---

## Summary

We will make Validibot's public API **explicitly org-scoped in the URL** and make workflows easy to reference by a
human-friendly slug while still supporting lookup by numeric database ID.

For workflows:

- Users can keep choosing a `slug` (already supported today). Slugs must never be empty in the database, even when they
  are auto-generated.
- Within an org, a workflow `slug` is a reserved identifier for a single "workflow family". Users cannot create two
  unrelated workflows in the same org with the same slug. New versions of the same workflow family intentionally reuse
  the same slug.
- In API routes where a workflow is identified by a single path segment, we will resolve that segment as:
  1) slug first (even if it looks numeric), 2) then numeric DB ID.
- When a slug maps to multiple versions, we will default to the **most recent version** (with an explicit version route
  to opt into older versions).

Because we don't have current users, we will make a clean cut without backward compatibility.

---

## Context

Validibot is multi-tenant (organizations/workspaces) and workflows are versioned.

Today:

- Most "app" URLs are **not org-scoped**; the current org is selected via session/user state.
- Many internal routes identify resources by **database PK** (e.g. workflows use `<int:pk>`).
- Public workflow "info" pages use a **UUID** in the URL (`workflows/<uuid>/info/`), which is correct for
  unguessability but not great for shareability or "type it from memory".
- The public API supports workflow lookup by either numeric ID or slug, but slug lookup can be ambiguous unless the
  caller uses query params like `?org=<org-slug>&version=<version>` (`validibot/workflows/views.py`).

This is workable, but it has real costs:

- URLs aren't reliably shareable between users because org scope lives in the session.
- It's harder to reason about permissions and scoping because the org isn't part of the request path.
- Workflow "slugs as identifiers" become awkward when slugs are not globally unique and workflows are versioned.
- UUID URLs are cumbersome for day-to-day use when the intent is "friendly link", not "unguessable secret".

This ADR focuses on the API first. The web UI changes are covered in a companion ADR.

---

## Goals

- Make the API URL shape communicate tenancy: "this request is about org X".
- Make API calls shareable and unambiguous without requiring `?org=...` query params.
- Keep workflow identifiers easy to type and stable in the API:
  - Prefer a user-chosen slug.
  - Still allow numeric DB IDs for debugging and migrations.
- Provide a clear "versioning story" for workflows:
  - Default to "latest" when the caller gives only a slug.
  - Make it easy to request an explicit version.
- Keep the implementation straightforward Django: path converters, explicit view lookups, and good tests.

---

## Non-goals

- Hiding resource existence or preventing guessing. This ADR optimizes for ease of use, not obscurity.
- Introducing "published" vs "draft" semantics (we may add this later, but it is out of scope here).
- Changing the worker's internal endpoints (Cloud Tasks/Cloud Run callbacks). Those should remain stable and
  infrastructure-focused.
- Backward compatibility shims (no current users).
- Unauthenticated API access (all API endpoints require authentication; public HTML pages are separate).

---

## Decision

### 1) Org-scoped routing for the public API

We will add a canonical org-scoped API namespace:

`/api/v1/orgs/<org_slug>/...`

This replaces "org disambiguation via query params" as the primary shape for multi-tenant endpoints.

Old endpoints will be removed entirely (no backward compatibility needed).

### 2) Workflow identifier resolution (slug first)

In routes that accept a single workflow identifier segment, we will accept:

- `slug` (human-friendly)
- numeric DB `id` (debug-friendly)

Resolution order:

1. Try as a slug first (even if `identifier.isdigit()`).
2. If no match and identifier is numeric, try as DB `id`.

This matches the "ease of use" goal and avoids surprising behavior when a user creates a workflow slug like `"123"`.

#### Slug uniqueness

Within an org, a workflow `slug` identifies a single workflow family (all versions share the same slug).

To support versioning, the database stores one row per version and enforces uniqueness at the `(org, slug, version)`
level:

```python
UniqueConstraint(
    fields=["org", "slug", "version"],
    name="uq_workflow_org_slug_version",
)
```

This means:

- The same slug can exist in different orgs.
- The same slug can have multiple versions within the same org (that is the versioning mechanism).

Product behavior (what users experience):

- Creating a brand new workflow must fail if the slug is already used in that org.
- Creating a new version of an existing workflow family reuses the same slug and generates a new version value.

#### Slug requirements (non-empty)

Because we want org slugs and workflow slugs to be used in URLs, we should enforce a simple invariant:

- `Organization.slug` and `Workflow.slug` must be non-empty strings in the database.

Practically, this means:

- We can keep "blank means auto-generate" in forms, but model save logic must ensure we never persist `""`.
- If `slugify(name)` produces an empty string (for example, a name that is only punctuation or characters that don't
  map cleanly), we should fall back to a safe generated slug (for example, a short random token like `wf-8f3a1c2d`).

This keeps URL generation reliable and avoids "/workflows//" style paths.

### 3) Workflow version format

Workflow versions must be either:

- **Integer**: `1`, `2`, `3`, etc.
- **Semantic version**: `1.0.0`, `2.1.3`, etc.

Arbitrary string labels are not permitted. This simplifies "latest" resolution and ordering.

Notes:

- Today `Workflow.version` is a free-form string and may be empty (`""`). As part of this ADR, we will backfill empty
  versions to a valid starting version (for example `1`) and enforce non-empty versions going forward.
- Today cloning uses a `Decimal`-based incrementer. To support the version format restriction above, we must update the
  clone/versioning logic to avoid producing non-integer values like `2.5`.
- For ordering, we will normalize integer versions by treating `N` as `N.0.0` so they can be compared consistently with
  semantic versions.

### 4) Workflow version defaults ("latest")

When a workflow is referenced by slug and multiple versions exist, the default must be predictable.

We will define "latest" as:

1. Filter to the org (`org_slug`) and workflow slug.
2. Prefer workflows that are not archived (and ideally active).
3. If multiple remain, choose the most recent version using:
   - Version comparison (normalize integer `N` as `N.0.0`; highest version wins)
   - Most recent `created` timestamp as a tiebreaker

We will also support requesting an explicit version via a versioned URL (see below).

### 5) Authentication required

All API endpoints require authentication. Unauthenticated users can only access:

- Public HTML pages (marketing, public workflow info pages)
- The JWKS endpoint for credential verification

---

## Proposed Routes

### Public API (authenticated)

Workflow endpoints under an org:

- List workflows (latest only by default): `GET /api/v1/orgs/<org_slug>/workflows/`
- Get a workflow (slug/id): `GET /api/v1/orgs/<org_slug>/workflows/<workflow_identifier>/`
- List workflow versions: `GET /api/v1/orgs/<org_slug>/workflows/<workflow_slug>/versions/`
- Get explicit workflow version: `GET /api/v1/orgs/<org_slug>/workflows/<workflow_slug>/versions/<version>/`
- Create a validation run (default version): `POST /api/v1/orgs/<org_slug>/workflows/<workflow_identifier>/runs/`
- Create a validation run (explicit version): `POST /api/v1/orgs/<org_slug>/workflows/<workflow_slug>/versions/<version>/runs/`

Note: The endpoint uses `runs/` (RESTful resource creation) rather than `launch/`. The UI can still use "Launch"
terminology in buttons and copy.

Validation run endpoints:

- List runs: `GET /api/v1/orgs/<org_slug>/runs/`
- Get a run: `GET /api/v1/orgs/<org_slug>/runs/<run_id>/` (run IDs are UUIDs)

### Response format

Responses will include a canonical `url` field and version metadata:

```json
{
  "id": 42,
  "slug": "my-workflow",
  "version": "3",
  "org_slug": "my-org",
  "is_active": true,
  "url": "/api/v1/orgs/my-org/workflows/my-workflow/"
}
```

---

## Data Model Changes

### Enforce non-empty slugs (workflow + org)

We already allow blank slugs in the UI so users can let the system auto-generate. That's fine, but we should treat
`""` as invalid persisted data.

As part of this change, we will ensure `Workflow.slug` and `Organization.slug` are always populated on save, and we will
backfill any historical records with empty slugs (if any exist).

### Enforce version format

Add validation to ensure `Workflow.version` is either an integer or semantic version string.

---

## Implementation Plan (High Level)

### Phase 1: Enforce non-empty slugs and version format

- Add guards so org/workflow slugs can't be persisted as empty strings.
- Add validation for version format (integer or semver).
- Backfill any historical records with empty slugs (if any exist).

### Phase 2: Add org-scoped API endpoints

- Add a new `/api/v1/orgs/<org_slug>/...` namespace.
- Move workflow endpoints into that namespace.
- Implement "slug first" workflow lookup and "latest version by default".
- Add explicit version endpoints.
- Change `launch/` to `runs/` endpoint.

### Phase 3: Update API docs and CLI

- Update OpenAPI and any published API docs.
- Update `validibot-cli` to use `/api/v1/orgs/<org_slug>/...` as the primary path and to support `--version`.

### Phase 4: Remove old routes

- Remove the old `/api/v1/workflows/<slug>/?org=...` endpoints entirely.
- Keep any truly internal-only endpoints (worker callbacks/execute) untouched.

---

## Code Changes Required

### Files to update for `launch/` → `runs/` rename

- `config/api_router.py` - Route definitions
- `validibot/workflows/views.py` - `WorkflowViewSet.start_validation` action name/URL
- `validibot-cli/` - CLI commands that call the launch endpoint
- API documentation
- Any frontend JavaScript that calls the API directly

---

## Testing Plan

Minimum tests to keep us safe while refactoring:

- Workflow identifier resolution:
  - Numeric-looking slug resolves as slug (not id).
  - id lookup still works.
  - Slug auto-generation never persists an empty slug.
- Version selection:
  - Multiple versions exist → default returns latest.
  - Explicit version route returns the requested version.
  - Invalid version format rejected.
- API routing:
  - Requests under `/api/v1/orgs/<org_slug>/...` scope queries to that org.
  - Unauthenticated requests return 401/403.
- Response format:
  - `url` field included in workflow responses.

---

## Consequences

### Positive

- URLs become shareable and predictable.
- Multi-tenant scoping is visible and easier to reason about.
- Workflow links become human-friendly without losing precision.
- API becomes simpler to use (no more required `?org=` disambiguation in the common case).
- RESTful `runs/` endpoint is more intuitive for API consumers.

### Negative / Risks

- We must ensure every API query is scoped by `org_slug` to avoid cross-tenant data leaks.
- Slug-first resolution means a numeric slug will "win" over numeric id lookup; this is intended but should be clearly
  documented for API consumers.

---

## Decisions Made

The following questions from the original draft have been resolved:

| Question | Decision |
|----------|----------|
| Slug uniqueness scope | Reserved per org (workflow family key), reused across versions |
| Version format | Integer or semantic version only (no arbitrary strings) |
| `launch/` vs `runs/` endpoint | Use `runs/` (RESTful); keep "Launch" in UI copy |
| Backward compatibility | None needed (no current users) |
| Unauthenticated API access | Not allowed (API requires auth) |
| Include `url` in responses | Yes |
