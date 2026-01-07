# ADR-2025-12-15: Invite-Only Free Access and Cross-Org Workflow Sharing

**Status:** Deprecated (superseded by ADR-2026-01-07)
**Date:** 2025-12-15
**Superseded By:** `../2026-01-07-guest-management-ui.md`
**Context:** Enable workflow authors to share workflows with external users without granting organization membership (seats), while controlling abuse/cost.

This ADR is deprecated. ADR-2026-01-07 replaces the invite-only policy and
introduces the current guest management and public workflow visibility model.

## Summary

We will support "cross-org workflow sharing" by introducing **Workflow Guests** (no seats) alongside existing **Organization Members** (seat-based). Workflow authors can invite external users (by email) to run specific workflows.

Free access is **invite-only**: users can only create accounts via invite tokens. Invited users are **auto-approved** and placed on the **Free Tier plan**, which does not include an organization of their own.

Public workflow execution is explicitly **off/hidden** for the initial rollout. The existing `make_info_public` remains an _info visibility_ concept, not a permission to execute. See Non-Goals for rationale; a future ADR may revisit this once abuse controls mature.

## Problem Statement

Validibot's current authorization model is organization-scoped and seat-based. That works for teams, but creates unnecessary friction for common workflow-sharing scenarios:

- Consultants need clients to run a workflow without consuming seats.
- Enterprises need external partners to validate data before submission.
- Early growth needs "try it via invite" without opening anonymous/public endpoints.

We need a model that lets external users run a workflow with tight safety controls, without turning every external user into an org member.

## Terminology

- **Organization Member**: A user with a `Membership` in an org (consumes a seat; covered by org subscription limits).
- **Workflow Guest**: A user who can access specific workflows via workflow-level grants (does _not_ consume a seat). Guests are on the Free Tier plan and do not have an organization of their own.
- **Free Tier plan**: A subscription plan with no organization. Users on this plan can only access workflows they've been explicitly invited to as Guests.

## Design Principles

- **Seats remain org-only**: Membership continues to be the only thing that consumes seats.
- **Author pays for shared usage**: Guest usage is billed/metered against the workflow owner's org.
- **Start closed, then open**: Invite-only first. Public execution can be revisited later.
- **Simple, auditable grants**: Prefer explicit per-workflow grants over clever implicit rules.
- **Cost control by default**: Rate limits and quotas must be enforceable per principal and per workflow.

## Product Requirements (Policy + UX)

### 1) Invite-only signup

- Users can only create accounts via invite tokens (no self-serve signup for free access).
- When an unrecognized email is invited to a workflow:
  - They receive an invite link (valid for 7 days) and can create an account.
  - Upon account creation, they are **auto-approved** and immediately granted access to the workflow(s) they were invited to.
  - Their account is placed on the **Free Tier plan** (no organization of their own).

This mirrors the Notion/Linear-style "invite-only" posture and reduces abuse during early go-to-market.

### 2) Members vs Guests (Slack-style)

- "Invite to Organization" creates a **Member** (seat required).
- "Invite to Workflow" creates a **Guest** (no seat).
- These invitations must be distinct in UI copy and in backend objects, so it is always clear whether a seat is being consumed.

### 3) Workflow sharing UI

In the workflow settings UI, we need an access management surface that supports:

- Listing:
  - Organization Members with access (inherited via roles and membership).
  - Workflow Guests (email, status: invited/accepted/expired).
- Actions:
  - Invite Guest to workflow (by email).
  - Remove access for a Guest.
  - Resend expired invites.
  - See invite acceptance status (pending, accepted, expired).

The UI should show expired invites and offer the ability to resend them.

### 4) Bulk grants (project/org shortcuts)

We want UX shortcuts like:

- "Grant this person access to all workflows in Project X"
- "Grant this person access to all workflows in this Organization"

Implementation detail: these should create **individual per-workflow grants in bulk**, not a special "project-level grant". This keeps auditing and revocation straightforward—removing a user from a project is just deleting their grants for that project's workflows.

### 5) Guest UI restrictions

Workflow Guests (Free Tier users) should have an intentionally limited product surface:

- Left nav: **Workflows** and **Validation Runs** only (no organization selector, since they have no org).
- Guests can only see their own runs, not org-wide run history.
- Guest access is read-only except:
  - launching runs for workflows they can access,
  - viewing their run results.
- Guests cannot:
  - see billing pages,
  - manage org members,
  - create or edit workflows,
  - manage credentials/integrations.

### 6) Seat visibility and enforcement

The Members management UI must show:

- seat limit,
- seats used,
- seats remaining.

The "Invite to Organization" flow must prevent inviting beyond the seat limit (server-side enforcement plus UI feedback).

## Technical Design (High Level)

### Authorization model

We treat access as a union:

- A user can execute a workflow if they are:
  - a Member in the workflow's org with execute permission, OR
  - a Workflow Guest with an access grant for that workflow.

Public workflow execution is out of scope for the initial release.

### Data model sketch

This ADR intentionally stays implementation-agnostic, but the minimum viable shape is:

- **WorkflowAccessGrant**

  - `workflow`
  - `principal` (user)
  - `granted_by`
  - `created_at`

- **WorkflowInvite** (or reuse/extend existing invite infrastructure)

  - target: workflow
  - invitee_email
  - status (pending/accepted/expired)
  - token
  - expires_at (default: 7 days from creation)

### Billing attribution

When a Guest launches a workflow run, metering and billing attribution must charge the workflow owner's org (not the guest).

### Rate limiting

We will enforce rate limits using DRF throttling with **separate throttle scopes** for member vs guest launches.

Existing context: the workflow launch API action already uses `throttle_scope="workflow_launch"` with `ScopedRateThrottle`.

This ADR expects us to introduce an explicit scope split:

- **Member launch scope**: keep using `workflow_launch` (current behavior).
- **Guest launch scope**: add `workflow_launch_guest`.
- **Public launch scope**: reserve `workflow_launch_public` (not enabled initially).

Implementation note (important): because the scope needs to depend on _who_ is launching (member vs guest), we cannot rely solely on a static `throttle_scope` string on the view. Instead, create a **custom throttle class** that extends `SimpleRateThrottle` and overrides `get_cache_key()` to:

1. Determine whether the authenticated user is a Member or Guest for the target workflow.
2. Return a cache key that incorporates the appropriate scope (`workflow_launch` vs `workflow_launch_guest`).

Example pattern:

```python
from rest_framework.throttling import SimpleRateThrottle

class WorkflowLaunchThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        workflow = view.get_object()
        if user_is_guest_for_workflow(request.user, workflow):
            self.scope = "workflow_launch_guest"
        else:
            self.scope = "workflow_launch"
        self.rate = self.get_rate()
        return self.cache_format % {
            "scope": self.scope,
            "ident": request.user.pk,
        }
```

Note: DRF does not have a `get_throttle_scope()` method. The standard extension points are `get_cache_key()` on throttle classes or `get_throttles()` on views.

Defaults should be configured via `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]` and remain env-driven, with guest defaults set materially lower than member defaults.

Initial release: org-level defaults only.

Future: per-workflow overrides (e.g., high-traffic shared workflows) can be layered by including `workflow_id` in the throttle cache key or by applying an additional per-workflow throttle.

## Rollout Plan

1. Implement workflow-level guest access grants and workflow invites.
2. Add workflow access management UI (listing, invite, remove, resend).
3. Add bulk-grant shortcuts implemented as per-workflow grants.
4. Add guest-scoped rate limits at org level.

## Future Considerations

The following are explicitly deferred to future releases:

- **Self-serve free tier signup**: Allow anyone to sign up for a Free Tier account without an invite. These users would require **superuser approval** (via Django admin) before they can access any workflows. Implementation: add an `is_approved` boolean on the User model, defaulting to `True` for all current signup paths (invite-based, org membership) but `False` for self-serve free tier signup. A new "Free Tier" plan would appear on the plans page. Approved free tier users could then access public workflows or workflows they're later invited to.
- **Access Groups**: Named sets of users that can be granted access to multiple workflows. Bulk grants (step 3 above) provide a simpler solution for the "100 users" problem initially.
- **Per-workflow rate limit overrides**: Authors can customize rate limits for specific high-traffic workflows.
- **Grant expiration**: Time-limited access grants with automatic revocation.
- **Run caps per grant**: Limit how many runs a specific guest can perform on a workflow.
- **Public execution**: Anonymous execution of workflows marked as public.

## Security and Abuse Considerations

- Invite-only limits account farming.
- Invite tokens expire after 7 days.
- Workflow owners must be able to revoke guest access immediately.
- Rate limits must prevent a single guest from burning an org's quota.
- All access changes should be auditable (who granted/revoked access, when).

## Non-Goals (Initial Release)

- Anonymous execution of workflows.
- A marketplace model where Guests pay directly.
- Making `make_info_public` imply executable permission.
- Self-serve signup without an invite (planned for future release; see Future Considerations).

## Decisions Made

1. **Approval model**: All invited users are auto-approved. Self-serve free tier signup (future) will require superuser approval via Django admin.
2. **Free Tier plan**: Guests are placed on a Free Tier plan with no organization of their own. They do not see an organization selector in the left nav.
3. **Access Groups**: Out of scope for initial release. Bulk grants provide a simpler solution.
4. **Per-workflow rate limits**: Out of scope for initial release. Org-level defaults only.
5. **Guest run visibility**: Guests see only their own runs, not org-wide run history.
6. **Grant expiration**: Out of scope for initial release.
7. **Invite token expiration**: 7 days. Expired invites shown in UI with resend option.
