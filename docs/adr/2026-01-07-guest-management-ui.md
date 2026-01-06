# ADR-2026-01-07: Guest Management UI

**Status:** Proposed
**Owners:** Platform / Frontend
**Related ADRs:**
- `docs/adr/2025-12-15-free-tier-and-workflow-sharing.md` (Guest access model)
- `docs/adr/2025-11-28-invite-based-signup-and-seat-management.md` (Member invitation patterns)
**Related code:**
- `validibot/users/models.py` (`PendingInvite`, `Membership`)
- `validibot/workflows/models.py` (`WorkflowAccessGrant`, `WorkflowInvite`)
- `validibot/members/views.py` (Member management patterns)
- `validibot/notifications/models.py` (`Notification`)

---

## Summary

We will add a "Guests" management section to the organization settings UI, positioned just below "Members".
This section allows org admins to invite external users as workflow guests, manage their workflow access,
and view pending/expired invitations.

The implementation reuses the existing `PendingInvite` model (with a new `invite_type` field) and
`WorkflowAccessGrant` model, following the same patterns as member invitations.

---

## Context

The guest access model was designed in ADR-2025-12-15, which introduced:
- `WorkflowAccessGrant` for granting users access to specific workflows
- `WorkflowInvite` for inviting external users to access a workflow

However, there's currently no admin UI for managing guests at the organization level. Admins need to:
- See all guests who have access to any workflow in their org
- Invite new guests and assign them to multiple workflows
- Modify which workflows a guest can access
- Revoke guest access entirely

The existing per-workflow sharing UI (from the workflow settings page) remains useful for quick,
single-workflow sharing, but doesn't provide the org-wide view admins need.

---

## Goals

- Provide a centralized "Guests" management UI parallel to the existing "Members" UI
- Allow multi-select workflow assignment when inviting guests
- Support modifying workflow access for existing guests
- Show pending and expired guest invitations with resend capability
- Send notifications when workflow access is granted or revoked

---

## Non-goals

- Replacing the per-workflow sharing UI (both UIs serve different use cases)
- Bulk guest import (CSV upload, etc.)
- Guest self-service (guests requesting access to workflows)

---

## Decision

### 1) Data model changes

**Extend `PendingInvite` with an `invite_type` field:**

```python
class InviteType(models.TextChoices):
    MEMBER = "member", "Organization Member"
    GUEST = "guest", "Workflow Guest"

class PendingInvite(TimeStampedModel):
    # ... existing fields ...
    invite_type = models.CharField(
        max_length=10,
        choices=InviteType.choices,
        default=InviteType.MEMBER,
    )
    # For guest invites: which workflows they'll get access to
    workflows = models.ManyToManyField(
        "workflows.Workflow",
        blank=True,
        related_name="pending_guest_invites",
    )
```

**Why reuse `PendingInvite` instead of `WorkflowInvite`?**

- Same invitation flow: email if user doesn't exist, notification if they do
- Same status lifecycle: PENDING → ACCEPTED/DECLINED/CANCELED/EXPIRED
- Same token-based acceptance mechanism
- Avoids duplicating invitation infrastructure

The existing `WorkflowInvite` model was designed for per-workflow invites from the workflow settings page.
We'll keep it for that use case but use `PendingInvite` with `invite_type=GUEST` for org-level guest management.

### 2) URL structure

New routes under `/app/orgs/<org_slug>/settings/`:

```
/guests/                           - Guest list view
/guests/invite/                    - Invite guest form (modal or page)
/guests/<guest_id>/                - Guest detail/edit view
/guests/<guest_id>/delete/         - Remove guest access
/guests/invites/<invite_id>/cancel/ - Cancel pending invite
/guests/invites/<invite_id>/resend/ - Resend expired invite
```

### 3) UI structure

**Guest List View** (`GuestListView`)

Displays:
- Active guests (users with any `WorkflowAccessGrant` in this org)
- Pending guest invitations (`PendingInvite` with `invite_type=GUEST`, status=PENDING)
- Expired/canceled invitations (with resend option for expired)

For each active guest, show:
- User email/name
- Number of workflows they can access
- "Edit" and "Remove" actions

For each pending invitation, show:
- Invitee email
- Workflows they'll get access to
- Invite status (pending/expired)
- "Cancel" or "Resend" actions

**Invite Guest Form** (`GuestInviteCreateView`)

Fields:
- Email/user search (same pattern as member invite)
- Workflow multi-select (checkboxes or multi-select dropdown)
  - Shows all active, non-archived workflows in the org
  - Grouped by project if projects exist

On submit:
- Creates `PendingInvite` with `invite_type=GUEST` and selected workflows
- If invitee is existing user: creates `Notification`
- If invitee is new: sends invitation email

**Guest Detail/Edit View** (`GuestUpdateView`)

Shows:
- Guest user info
- Current workflow access (list of workflows with checkboxes)
- Ability to add/remove workflow access

When workflows are added/removed:
- Create/deactivate `WorkflowAccessGrant` records
- Create notification for the guest (see section 5)

### 4) Invitation acceptance flow

When a guest invite is accepted (via notification or email link):

1. For each workflow in `invite.workflows.all()`:
   - Create `WorkflowAccessGrant(workflow=workflow, user=user, granted_by=invite.inviter)`
2. Set `invite.status = ACCEPTED`
3. Notify the inviter that the invite was accepted

This mirrors the member acceptance flow but creates grants instead of membership.

### 5) Notifications for access changes

When an admin modifies a guest's workflow access (not during initial invite acceptance):

**Workflows added:**
```python
Notification.objects.create(
    user=guest_user,
    org=org,
    type=Notification.Type.SYSTEM_ALERT,  # or new type: GUEST_ACCESS_CHANGED
    payload={
        "action": "workflows_added",
        "workflow_names": ["Workflow A", "Workflow B"],
        "changed_by": admin_user.id,
    },
)
```

**Workflows removed:**
```python
Notification.objects.create(
    user=guest_user,
    org=org,
    type=Notification.Type.SYSTEM_ALERT,
    payload={
        "action": "workflows_removed",
        "workflow_names": ["Workflow C"],
        "changed_by": admin_user.id,
    },
)
```

The notification template renders these appropriately:
- "You now have access to Workflow A and Workflow B in Acme Corp"
- "Your access to Workflow C in Acme Corp has been removed"

### 6) Edge cases

**Guest upgraded to member:**
- When a user is added as an org member (via `Membership`), remove their guest status:
  - Deactivate all `WorkflowAccessGrant` records for workflows in that org
  - Cancel any pending guest invites for that user in that org
- Members have broader access via roles, so workflow-specific grants are redundant

**Workflow deleted:**
- `WorkflowAccessGrant` records are cascade-deleted with the workflow
- No notification needed (workflow simply disappears from guest's view)

**Workflow archived:**
- Grants remain but workflow is hidden from guest's workflow list
- If unarchived, access is restored

**Guest has no remaining workflow access:**
- After removing their last workflow grant, the guest no longer appears in the guest list
- They remain a user but have no access to this org's resources
- Consider: Should we show "former guests" or just let them disappear?

**Decision:** Guests with no active grants simply disappear from the list. If re-invited later,
they go through the normal invite flow again (which will immediately grant access since they
already have an account).

### 7) Permissions

Guest management requires the `GUEST_MANAGE` permission (new), which should be included in
the ADMIN role by default.

```python
class PermissionCode(str, Enum):
    # ... existing ...
    GUEST_MANAGE = "guest_manage"
```

---

## Implementation Plan

1. **Model changes**
   - Add `invite_type` field to `PendingInvite`
   - Add `workflows` M2M field to `PendingInvite`
   - Add migration

2. **Views and forms**
   - `GuestListView` - list guests and pending invites
   - `GuestInviteCreateView` - invite form with workflow multi-select
   - `GuestUpdateView` - edit workflow access
   - `GuestDeleteView` - remove all access
   - `GuestInviteCancelView` - cancel pending invite
   - `GuestInviteResendView` - resend expired invite

3. **Templates**
   - `guests/guest_list.html`
   - `guests/guest_invite_form.html`
   - `guests/guest_detail.html`
   - Partials for HTMX interactions

4. **Notification templates**
   - Update notification rendering to handle guest access change payloads

5. **Accept flow updates**
   - Modify invite acceptance to handle `invite_type=GUEST`
   - Create `WorkflowAccessGrant` records on acceptance

6. **Member flow updates**
   - When creating membership, clean up guest grants/invites for that user

7. **Navigation**
   - Add "Guests" link in org settings nav, below "Members"

---

## Alternatives Considered

### A) Separate `GuestInvite` model

Could create a dedicated model for guest invitations instead of extending `PendingInvite`.

**Pros:** Cleaner separation, no need to modify existing model
**Cons:** Duplicates invitation infrastructure, more code to maintain

**Decision:** Extend `PendingInvite` to reuse existing patterns.

### B) Use `WorkflowInvite` for all guest invites

The existing `WorkflowInvite` model handles per-workflow invitations.

**Pros:** Already exists
**Cons:** Designed for single-workflow invites; would need to create multiple invites for
multi-workflow assignment, complicating the UX

**Decision:** Keep `WorkflowInvite` for per-workflow sharing UI; use `PendingInvite` with
`invite_type=GUEST` for org-level guest management.

### C) Notification type for access changes

Could add a new `Notification.Type.GUEST_ACCESS_CHANGED` instead of using `SYSTEM_ALERT`.

**Pros:** More semantic, easier to filter
**Cons:** Another type to maintain

**Decision:** Start with `SYSTEM_ALERT` and the `payload["action"]` field. Can add a dedicated
type later if needed.

---

## Security Considerations

- Only users with `GUEST_MANAGE` permission can invite/modify/remove guests
- Guest invites are scoped to org; cannot invite guests to workflows in other orgs
- Invitation tokens expire after 7 days (same as member invites)
- All guest access changes are auditable via `WorkflowAccessGrant` timestamps and `granted_by`

---

## Open Questions

1. **Should we show "former guests"** (users who previously had access but no longer do)?
   - Current decision: No, they simply disappear from the list
   - Alternative: Show in a separate "Former guests" section for audit purposes

2. **Notification for complete access removal?**
   - When all workflow access is removed, should we send a notification saying "Your guest access
     to Acme Corp has been removed"?
   - Current decision: Yes, send notification with `action: "access_revoked"`
