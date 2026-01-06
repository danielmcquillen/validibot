# ADR-2026-01-06: Org-scoped Web URLs

**Status:** Proposed
**Owners:** Platform / Frontend
**Related ADRs:**
- `docs/adr/2026-01-06-org-scoped-routing-and-versioned-workflow-identifiers.md` (API companion)
- `docs/adr/2025-12-22-cli-api-support.md`
**Related code:**
- `config/urls_web.py`
- `validibot/core/utils.py` (`reverse_with_org`)
- `validibot/core/templatetags/core_tags.py` (`org_url`)
- `validibot/workflows/urls.py`
- `validibot/projects/urls.py`
- `validibot/validations/urls.py`
- `validibot/users/mixins.py`
- `validibot/users/scoping.py`

---

## Summary

We will update the authenticated web UI to use **org-scoped URLs** (`/app/orgs/<org_slug>/...`) for consistency
with the API changes and to make URLs shareable, bookmarkable, and unambiguous.

This is the companion ADR to the API routing changes. While the API ADR focuses on programmatic access, this ADR
addresses the user-facing web interface.

Validibot also has "workflow guests": authenticated users who have been granted access to launch specific org workflows,
but who are not members of those organizations. Rather than creating separate guest-specific routes, we surface shared
and public workflows within the user's **personal organization** using filter controls.

Since we have no current users, we will make a clean cut without backward compatibility shims.

---

## Context

Today, the authenticated web UI uses session-based org selection:

- URLs like `/app/workflows/`, `/app/projects/` don't include the org
- The "current org" is stored in the user's session or derived from their default org
- Switching orgs happens via a dropdown in the UI, which updates the session

We also support workflow guests (authenticated users with `WorkflowAccessGrant`s) who can access workflows from
organizations they don't belong to.

This works but has costs:

1. **URLs aren't shareable** - Sharing `/app/workflows/42/` with a colleague doesn't work if they have a different
   default org selected
2. **Bookmarks break** - A bookmark to a workflow might show different content depending on session state
3. **Deep linking fails** - External links (from emails, Slack, docs) can't target a specific org context
4. **Inconsistent with API** - After implementing org-scoped API routes, the web UI will feel inconsistent
5. **Guest UX is unclear** - No natural place for guests to discover shared workflows

---

## Goals

- Make web URLs **explicitly org-scoped** so they're shareable and bookmarkable
- Maintain **consistency with the API** URL structure
- Preserve the **org-switching UX** (dropdown still works, just updates the URL)
- Provide a clear, intuitive place for workflow guests to discover and launch shared workflows

---

## Non-goals

- Backward compatibility redirects (no current users)
- Changing public/unauthenticated routes (marketing, public workflow info pages)
- Multi-org views in team orgs (showing data from multiple orgs on one page for team contexts)

---

## Decision

### 1) New URL structure for authenticated app routes

Current:
```
/app/workflows/
/app/workflows/42/
/app/projects/
/app/validations/<run_id>/  # run_id is a UUID
```

New:
```
/app/orgs/<org_slug>/workflows/
/app/orgs/<org_slug>/workflows/42/
/app/orgs/<org_slug>/projects/
/app/orgs/<org_slug>/validations/<run_id>/  # run_id is a UUID
```

Old org-member routes will be removed entirely.

### 2) Org resolution from URL

Views will extract `org_slug` from the URL path instead of session. The org-scoping middleware/mixin will:

1. Resolve `org_slug` to an Organization object
2. Verify the user is an active member of that org (or is a superuser)
3. Set `request.active_org` and synchronize `active_org_id` in the session/user so existing code keeps working
4. Return 404 if org doesn't exist, 403 if user lacks access

### 3) Org-switching behavior

The org dropdown in the navbar will:
- Show available orgs as before
- On selection, **redirect to the equivalent URL** in the new org context
- Example: Switching from `acme` to `globex` while on `/app/orgs/acme/workflows/` redirects to
  `/app/orgs/globex/workflows/`

### 4) Personal organization as the hub for shared/public workflows

Every user has a **personal organization** (auto-created on signup). The personal org serves as the central place
where users can discover and launch workflows they don't own:

- **My Workflows**: Workflows the user created in their personal org
- **Shared with me**: Workflows from other orgs where the user has a `WorkflowAccessGrant`
- **Public**: Publicly available workflows (marked `is_public=True`)

When viewing the workflow list in a personal org, the UI shows filter controls to toggle these categories. When
results include shared or public workflows, an "Org" column appears in the table/card view to show which org owns
each workflow.

When viewing a **team org**, only that org's workflows appear (no filters, no shared/public workflows from elsewhere).

### 5) Run ownership for shared/public workflows

When a guest user launches a shared or public workflow:

- The **validation run belongs to the workflow's owning org** (for billing, quotas, audit purposes)
- The **guest user is recorded as the launcher** (`launched_by` field)
- The guest user **sees the run in their personal validations list** (filtered by `launched_by=user`)

This means:
- The owning org pays for and tracks the run
- The guest user can view their own runs without needing membership in the owning org
- The validations list in a personal org shows runs the user launched (across all orgs)
- The validations list in a team org shows runs belonging to that org

### 6) Routes that remain org-agnostic

Some authenticated routes don't need org scoping because they're user-specific:

- `/app/users/profile/` - User's own profile
- `/app/users/api-key/` - User's API key management
- `/app/users/email/` - User's email settings

---

## Proposed URL Structure

### Org-scoped routes (all orgs including personal)

- `/app/orgs/<org_slug>/dashboard/`
- `/app/orgs/<org_slug>/workflows/...`
- `/app/orgs/<org_slug>/projects/...`
- `/app/orgs/<org_slug>/validations/...` (run IDs are UUIDs)
- `/app/orgs/<org_slug>/members/...`
- `/app/orgs/<org_slug>/billing/...`
- `/app/orgs/<org_slug>/tracking/...`

### Org-agnostic authenticated routes

- `/app/` redirects to the user's default org (typically their personal org)
- `/app/users/...`, `/app/notifications/...`, `/app/help/...`

### Personal org workflow list behavior

When the current org is a personal org, the workflow list includes additional capabilities:

1. **Filter controls** (checkbox group):
   - ☑ My Workflows (workflows owned by this personal org)
   - ☑ Shared with me (workflows with `WorkflowAccessGrant` for this user)
   - ☑ Public (workflows marked `is_public=True`)

2. **Conditional "Org" column**: When the result set includes shared or public workflows, display an "Org" column
   showing the owning organization for each workflow.

3. **Default filter state**: All three filters enabled by default. Filter state persists in session or URL query
   params for bookmarkability.

4. **UI component**: Use a compact filter chip/pill group or collapsible filter panel that doesn't dominate the view.
   Example: `[✓ My Workflows] [✓ Shared] [✓ Public]`

### Personal org validations list behavior

When the current org is a personal org, the validations list shows:
- Runs launched by the current user (regardless of which org owns them)

This allows guest users to see their own runs from shared/public workflows without needing org membership.

When the current org is a **team org**, the validations list shows:
- Runs belonging to that org (regardless of who launched them)

---

## Implementation Plan

### Phase 1: Ensure personal org auto-creation

- Verify every user has a personal org (auto-created on signup)
- Add migration to backfill personal orgs for any existing users without one
- Add `Organization.is_personal` flag (or derive from `org.owner == user` and single-member check)

### Phase 2: Update URL configuration

- Replace old `/app/workflows/` etc. routes with `/app/orgs/<org_slug>/workflows/`
- Add `/app/` redirect to default org (personal org for new users)
- Update URL namespaces

### Phase 3: Update views

- Add `OrgScopedMixin` (or middleware) to all org-scoped views to set `request.active_org` from `org_slug`
- Update `get_queryset()` to filter by URL org
- For personal orgs: extend workflow queryset to include shared and public workflows based on filter params
- For personal orgs: extend validations queryset to filter by `launched_by=user` instead of `org=org`

### Phase 4: Add filter controls to workflow list

- Add filter checkbox UI component (My Workflows / Shared with me / Public)
- Only show filters when current org is a personal org
- Persist filter state in session or URL query params (`?filter=mine,shared,public`)
- Add conditional "Org" column when result set includes non-owned workflows

### Phase 5: Update templates

- Prefer `{% org_url %}` for internal links and `reverse_with_org()` in Python code so we can inject `org_slug`
  automatically during the migration.
- Update navbar org-switcher to construct new URLs
- Add `current_org` to template context via context processor

### Phase 6: Update JavaScript

- Update any JS that constructs URLs to include org slug
- Update HTMX targets if needed
- Add filter toggle interactivity (HTMx or vanilla JS)

---

## Template and View Changes

### URL generation in templates (`org_url`)

Use `{% load core_tags %}` in templates that call `{% org_url %}`.

Before:
```django
<a href="{% url 'workflows:workflow_detail' pk=workflow.pk %}">
```

After:
```django
<a href="{% org_url 'workflows:workflow_detail' pk=workflow.pk %}">
```

This uses `validibot/core/templatetags/core_tags.py::org_url`, which calls `validibot/core/utils.py::reverse_with_org`.
During the migration, we can update `reverse_with_org()` to inject `org_slug` from `request.active_org` when the target
route requires it.

### View mixin recommendation (`request.active_org`)

Recommendation: keep the existing "active org" convention (`request.active_org`, `active_org_id` in session, and
`user.current_org`) and set those values from the URL. This lets us reuse the existing queryset filtering and template
context patterns with minimal churn.

### View mixin for org resolution (recommended shape)

```python
class OrgScopedMixin:
    """Resolve org from org_slug URL kwarg and sync request.active_org/session."""

    def dispatch(self, request, *args, **kwargs):
        org_slug = kwargs.get("org_slug")
        organization = get_object_or_404(Organization, slug=org_slug)

        is_member = request.user.is_superuser or request.user.memberships.filter(
            org=organization,
            is_active=True,
        ).exists()
        if not is_member:
            raise PermissionDenied("You don't have access to this organization")

        request.active_org = organization
        request.session["active_org_id"] = organization.id
        request.user.set_current_org(organization)

        self.org = organization
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        # Scope queryset to current org
        return super().get_queryset().filter(org=self.org)
```

---

## Testing Plan

- **URL resolution**: Verify org is extracted from URL correctly
- **Permission checks**: Verify 403 for orgs user can't access, 404 for non-existent orgs
- **Org switching**: Verify dropdown redirects to equivalent URL in new org
- **Link generation**: Verify all internal links include org_slug
- **Entry point**: Verify `/app/` redirects to default org
- **Personal org auto-creation**: Verify new users get a personal org on signup
- **Personal org workflow filters**: Verify filter controls appear only in personal org context
- **Shared workflows**: Verify users can see workflows shared with them via `WorkflowAccessGrant`
- **Public workflows**: Verify users can see public workflows in their personal org
- **Run ownership**: Verify runs from shared/public workflows belong to the workflow's owning org
- **Run visibility**: Verify guest users see their launched runs in personal org validations list
- **Org column**: Verify "Org" column appears when result set includes shared/public workflows

---

## Consequences

### Positive

- **Shareable URLs** - Links work correctly regardless of recipient's session state
- **Bookmarkable** - Bookmarks always go to the same org context
- **Deep linking** - External systems can link directly to specific org resources
- **Consistency** - Web UI matches API URL structure
- **Debugging** - Easier to see which org a request targets from logs/URLs
- **Simpler code** - No session-based org switching logic to maintain
- **Unified guest UX** - Guests discover shared/public workflows in one place (personal org)
- **Clear run ownership** - Runs belong to workflow owner (for billing), visible to launcher (for UX)

### Negative

- **Longer URLs** - Every authenticated route gains `/orgs/<org_slug>` prefix
- **Template changes** - Template churn unless we lean on `{% org_url %}` + `reverse_with_org()` during migration
- **Personal org complexity** - Must ensure every user has a personal org; adds a data model invariant
- **Public workflow list size** - Could be large; may need pagination or search (future enhancement)

---

## Decisions Made

| Question | Decision |
|----------|----------|
| Dashboard scope | Yes, org-scoped: `/app/orgs/<org_slug>/dashboard/` |
| Entry point for multi-org users | Redirect to default org (personal org for new users) |
| Guest workflow access | Via personal org with filter controls (not separate routes) |
| Run ownership for shared workflows | Belongs to workflow's owning org; visible to launcher |
| Filter UI | Checkbox chips: My Workflows, Shared with me, Public |
| Org column display | Shown conditionally when result set includes non-owned workflows |

---

## Relationship to API ADR

This ADR should be implemented **after** the API routing ADR (`2026-01-06-org-scoped-routing-and-versioned-workflow-identifiers.md`)
to ensure:

1. The org resolution logic is shared/consistent
2. The URL patterns follow the same conventions
3. We can reuse the slug validation and lookup code

The API changes are lower-risk (fewer URLs, no templates) and establish the patterns we'll follow here.
