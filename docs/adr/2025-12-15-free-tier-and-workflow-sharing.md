# ADR: Invite-Only Free Access and Cross-Org Workflow Sharing

**Date:** 2025-12-15
**Status:** Proposed
**Context:** Enable workflow authors to share workflows with external users without granting organization membership (seats), while controlling abuse/cost.

## Summary

We will support "cross-org workflow sharing" by introducing **Workflow Guests** (no seats) alongside existing **Organization Members** (seat-based). Workflow authors can invite external users (by email) to run specific workflows.

Free access is **invite-only**: users can only create accounts via invite tokens. The inviter auto-approves their invitees for the specific workflow(s) they were invited to.

Public workflow execution is explicitly **off/hidden** for the initial rollout. The existing `make_info_public` remains an _info visibility_ concept, not a permission to execute. See Non-Goals for rationale; a future ADR may revisit this once abuse controls mature.

## Problem Statement

Validibot's current authorization model is organization-scoped and seat-based. That works for teams, but creates unnecessary friction for common workflow-sharing scenarios:

- Consultants need clients to run a workflow without consuming seats.
- Enterprises need external partners to validate data before submission.
- Early growth needs "try it via invite" without opening anonymous/public endpoints.

We need a model that lets external users run a workflow with tight safety controls, without turning every external user into an org member.

## Terminology

- **Organization Member**: A user with a `Membership` in an org (consumes a seat; covered by org subscription limits).
- **Workflow Guest**: A user who can access specific workflows via workflow-level grants (does _not_ consume a seat).

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
  - Upon account creation, they are automatically granted access to the workflow(s) they were invited to.
  - The inviter implicitly approves them for those specific workflows.

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

Workflow Guests should have an intentionally limited product surface:

- Left nav: **Workflows** and **Validation Runs** only.
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

We will enforce rate limits by scope:

- **Members**: generous defaults
- **Guests**: tighter defaults
- **Public**: not enabled initially (but we design with a placeholder scope)

Rate limits will use org-level defaults only for the initial release.

We already use DRF throttling and scoped throttle rates. This work extends the current throttle scope set to include guest-oriented scopes.

## Rollout Plan

1. Implement workflow-level guest access grants and workflow invites.
2. Add workflow access management UI (listing, invite, remove, resend).
3. Add bulk-grant shortcuts implemented as per-workflow grants.
4. Add guest-scoped rate limits at org level.

## Future Considerations

The following are explicitly deferred to future releases:

- **Self-serve free signup**: Allow users to create free accounts without an invite, with access limited to public workflows only. This will be enabled once abuse controls and support tooling are mature.
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

1. **Approval model**: The inviter auto-approves their invitees for the specific workflow(s) they were invited to. No separate approval step needed.
2. **Access Groups**: Out of scope for initial release. Bulk grants provide a simpler solution.
3. **Per-workflow rate limits**: Out of scope for initial release. Org-level defaults only.
4. **Guest run visibility**: Guests see only their own runs, not org-wide run history.
5. **Grant expiration**: Out of scope for initial release.
6. **Invite token expiration**: 7 days. Expired invites shown in UI with resend option.
