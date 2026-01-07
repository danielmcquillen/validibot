# ADR-2026-01-07: Guest Management and Workflow Visibility

**Status:** Proposed
**Owners:** Platform / Frontend
**Supersedes:**

- `docs/adr/completed/2025-12-15-free-tier-and-workflow-sharing.md` (invite-only guest access model)
  **Related ADRs:**
- `docs/adr/2025-11-28-invite-based-signup-and-seat-management.md` (Member invitation patterns)
  **Related code:**
- `validibot/users/models.py` (`PendingInvite`, `Membership`)
- `validibot/workflows/models.py` (`WorkflowAccessGrant`, `WorkflowInvite`, `GuestInvite`)
- `validibot/notifications/models.py` (`Notification`)
- `validibot/members/views.py` (Member management patterns)

---

## Summary

This ADR covers three related features:

1. **Workflow visibility model** - A simple two-level system (private vs public) that determines
   who can launch a workflow
2. **Org-level guest management** - A "Guests" section in org settings for managing all guests
   across the organization
3. **Per-workflow sharing UI** - A "Sharing" tab in workflow settings for managing access to
   individual workflows

It also replaces the invite-only policy with public execution for authenticated users, and
keeps guest invites separate from member invites so the logic and notifications stay clear.

---

## Context

The guest access model was designed in ADR-2025-12-15, which introduced:

- `WorkflowAccessGrant` for granting users access to specific workflows
- `WorkflowInvite` for inviting external users to access a workflow

However, there's currently no UI for managing guests. The backend models exist (`WorkflowAccessGrant`,
`WorkflowInvite`) but no views were built to use them.

This ADR supersedes the invite-only policy from 2025-12-15. We now allow public workflows for
authenticated users so Free Tier users can launch workflows without waiting for an invite.
Invite-only signup is removed for Free Tier accounts; access is governed by workflow visibility
and guest/member grants.

---

## Goals

- Define a clear, simple workflow visibility model
- Provide org-level guest management UI (parallel to "Members")
- Provide per-workflow sharing UI for fine-grained access control
- Support public workflows for authenticated users (including Free Tier users)
- Send notifications when workflow access changes

---

## Non-goals

- Complex multi-level visibility (workspace/team/project scopes)
- Per-member workflow restrictions (members see all org workflows)
- Bulk guest import (CSV upload, etc.)
- Guest self-service (guests requesting access to workflows)
- Inviting guests to an organization (guests are workflow-scoped only)
- Anonymous workflow execution

---

## Decision

### 1) Workflow Visibility Model

Workflows have a simple two-level visibility:

| Visibility            | `is_public` | Who can launch                                                                      |
| --------------------- | ----------- | ----------------------------------------------------------------------------------- |
| **Private** (default) | `False`     | Org members with `WORKFLOW_LAUNCH` permission, OR guests with `WorkflowAccessGrant` |
| **Public**            | `True`      | Any authenticated user                                                              |

**Data model change:**

```python
class Workflow(TimeStampedModel):
    # Existing field, renamed (controls info page visibility, not execution)
    make_info_page_public = models.BooleanField(default=False)

    # NEW: Controls who can launch the workflow
    is_public = models.BooleanField(
        default=False,
        help_text=_("If true, any authenticated user can launch this workflow."),
    )
```

**Access rules:**

- **Private workflow**: Requires org membership OR explicit `WorkflowAccessGrant`
- **Public workflow**: Any logged-in user can launch (Free Tier users included)
- **Billing**: All usage billed to the workflow owner's org (regardless of who launches)

When a workflow is set to public, we automatically set `make_info_page_public=True` so the public
listing and info page stay in sync. The info page can still be public while execution is private.

**Why two levels instead of more?**

Apps like Notion have 4+ visibility levels, which creates confusion. Users forget what they set.
Two levels (private/public) covers the core use cases:

- Teams sharing internally (private)
- Authors publishing for anyone to use (public)

### 2) Org-Level Guest Management UI

A "Guests" section in org settings, positioned just below "Members".

The layout mirrors the Members page: a guest list on the left and an invite/current invitations
card on the right, using the same type-ahead search pattern.

**URL structure:**

```
/app/orgs/<org_slug>/settings/guests/                    - Guest list
/app/orgs/<org_slug>/settings/guests/invite/             - Invite guest form
/app/orgs/<org_slug>/settings/guests/<user_id>/          - Guest detail/edit
/app/orgs/<org_slug>/settings/guests/<user_id>/delete/   - Remove guest
/app/orgs/<org_slug>/settings/guests/invites/<id>/cancel/ - Cancel invite
/app/orgs/<org_slug>/settings/guests/invites/<id>/resend/ - Resend invite
```

Guests are never invited to the organization itself. They are only invited to all or a subset of
workflows in the org.

**Guest List View:**

Displays:

- Active guests (users with any `WorkflowAccessGrant` in this org)
- Pending guest invitations (from `GuestInvite` and `WorkflowInvite`)
- Expired invitations (with resend option)

Admins see the full org view. Authors see a scoped view limited to workflows they authored.
Counts and guest listings are filtered to the workflows in scope for the viewer. The guest list
header and invite card include counts for active guests and pending invitations.

For each active guest:

- User email/name
- Number of workflows they can access (e.g., "3 workflows")
- "Edit" and "Remove" actions

For each pending invitation:

- Invitee email
- Workflows they'll get access to
- Invite status badge (pending/expired)
- "Cancel" or "Resend" actions

**Invite Guest Form:**

Fields:

- Email/user search (same autocomplete pattern as member invite)
- Workflow multi-select (checkboxes grouped by project)
  - Shows all active, non-archived workflows the inviter can manage
  - Public workflows are labeled and can remain selected so access survives a
    public -> private switch
  - Admins can pick an "All workflows (current)" shortcut
  - New workflows are not auto-shared; admins can update guest access later
  - Authors only see workflows they authored

On submit:

- Creates a `GuestInvite` with the selected workflows and scope
- If invitee is existing user: creates a guest invite notification
- If invitee is new: sends invitation email

**Guest Detail/Edit View:**

Shows:

- Guest user info (email, name)
- Current workflow access (checkboxes for each private workflow)
- Add/remove workflow access with save button

Changes trigger notifications to the guest.

### 3) Per-Workflow Sharing UI

A "Sharing" tab in workflow settings for managing access to that specific workflow.

Admins and the workflow's author can manage this tab. Other members can view sharing state but
cannot add or remove guests.

The workflow detail page includes a "Sharing" button in the top navigation, positioned between
"Launch" and "View Validations", which links to this sharing screen.

**URL structure:**

```
/app/orgs/<org_slug>/workflows/<pk>/sharing/              - Sharing settings
/app/orgs/<org_slug>/workflows/<pk>/sharing/invite/       - Invite guest to this workflow
/app/orgs/<org_slug>/workflows/<pk>/sharing/<grant_id>/revoke/ - Revoke access
```

**Sharing Settings View:**

Two sections:

**1. Visibility Section:**

```
┌─────────────────────────────────────────────────────────┐
│ Visibility                                              │
│                                                         │
│ ○ Private - Only org members and invited guests         │
│ ● Public  - Any logged-in user can launch               │
│                                                         │
│ [Save]                                                  │
└─────────────────────────────────────────────────────────┘
```

Changing visibility:

- Private → Public: Existing guest grants remain (but are now redundant)
- Public → Private: Only org members and existing guests retain access

**2. Guest Access Section (only shown for private workflows):**

Section headers show counts for active guests and pending invitations.

```
┌─────────────────────────────────────────────────────────┐
│ Guest Access                                            │
│                                                         │
│ People with access to this workflow:                    │
│                                                         │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ alice@example.com          Added Jan 5   [Remove]   │ │
│ │ bob@contractor.co          Added Jan 3   [Remove]   │ │
│ └─────────────────────────────────────────────────────┘ │
│                                                         │
│ Pending invitations:                                    │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ carol@client.com   Pending   [Cancel] [Resend]      │ │
│ └─────────────────────────────────────────────────────┘ │
│                                                         │
│ [+ Invite Guest]                                        │
└─────────────────────────────────────────────────────────┘
```

**Invite Guest to Workflow:**

Simple form:

- Email input (with autocomplete for existing users)
- Submit creates `WorkflowInvite` for this single workflow
- Uses existing `WorkflowInvite` model (not `PendingInvite`)
- Uses the guest invite notification type with a workflow invite template

This is the quick path for "I want to share this one workflow with someone."

Workflow list cards and table rows show the number of active guests for each workflow
(`WorkflowAccessGrant` with `is_active=True`). This makes shared workflows easy to spot at a glance.

### 4) Data Model Changes

**Add a dedicated `GuestInvite` model for org-level guest invites:**

```python
class GuestInvite(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        ACCEPTED = "ACCEPTED", _("Accepted")
        DECLINED = "DECLINED", _("Declined")
        CANCELED = "CANCELED", _("Canceled")
        EXPIRED = "EXPIRED", _("Expired")

    class Scope(models.TextChoices):
        ALL = "ALL", _("All workflows in org")
        SELECTED = "SELECTED", _("Selected workflows")

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="guest_invites",
    )
    inviter = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sent_guest_invites",
    )
    invitee_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="received_guest_invites",
        null=True,
        blank=True,
    )
    invitee_email = models.EmailField(
        blank=True,
    )
    scope = models.CharField(
        max_length=16,
        choices=Scope.choices,
        default=Scope.SELECTED,
    )
    workflows = models.ManyToManyField(
        "workflows.Workflow",
        blank=True,
        related_name="guest_invites",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    expires_at = models.DateTimeField()
    token = models.UUIDField(default=uuid4, editable=False)
```

`PendingInvite` remains the member invite model. Guest invites never create memberships.
GuestInvite scope controls whether we grant access to all current workflows or the selected subset.
Accepted invites expand into per-workflow `WorkflowAccessGrant` rows.

**Add `is_public` to Workflow:**

```python
class Workflow(TimeStampedModel):
    is_public = models.BooleanField(
        default=False,
        help_text=_("If true, any authenticated user can launch this workflow."),
    )
```

**Rename `make_info_public` to `make_info_page_public`:**

This keeps terminology clear and avoids implying execution access.

`WorkflowAccessGrant` remains per-workflow only. There is no org-wide grant scope.

**Keep existing `WorkflowInvite` for per-workflow invites:**

The existing `WorkflowInvite` model is used for the per-workflow sharing UI.
It handles single-workflow invitations with its own acceptance flow.

### 5) Access Control Updates

Update `Workflow.can_execute()` to include public workflows and guest grants:

```python
def can_execute(self, *, user: User) -> bool:
    if not self.is_active:
        return False
    if not user or not user.is_authenticated:
        return False

    # Public workflows: any authenticated user
    if self.is_public:
        return True

    # Org member check
    if user.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, self):
        return True

    # Guest grant check
    return self.access_grants.filter(
        user=user,
        is_active=True,
    ).exists()
```

### 6) Invitation Acceptance Flows

**Org-level guest invite (`GuestInvite`):**

1. User accepts via notification or email link
2. Resolve the workflow set (all current workflows in org, or the selected subset)
3. For each workflow in the resolved set:
   - Create `WorkflowAccessGrant(workflow=workflow, user=user, granted_by=invite.inviter)`
4. Set `invite.status = ACCEPTED`
5. Notify the inviter

**Per-workflow invite (`WorkflowInvite`):**

1. User accepts via notification or email link
2. Create single `WorkflowAccessGrant` for that workflow
3. Set `invite.status = ACCEPTED`
4. Notify the inviter

### 7) Notifications

**Guest invites use a dedicated notification type and template:**

```python
class NotificationType(models.TextChoices):
    MEMBER_INVITE = "member_invite", _("Member invite")
    GUEST_INVITE = "guest_invite", _("Guest invite")
    SYSTEM_ALERT = "system_alert", _("System alert")
```

Guest invite payloads include workflow context (names or counts) so the copy clearly
distinguishes guest access from org membership. Use separate templates for org-level guest
invites vs per-workflow guest invites.

Guest notifications link to the invite record so accept/decline routes are unambiguous:

```python
class Notification(models.Model):
    invite = models.ForeignKey(
        PendingInvite,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    guest_invite = models.ForeignKey(
        GuestInvite,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    workflow_invite = models.ForeignKey(
        WorkflowInvite,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
```

Exactly one invite field is set per notification. Member invites use `invite`, org-level guest
invites use `guest_invite`, and per-workflow guest invites use `workflow_invite`.
Enforce this with model validation and a database check constraint.

**When access is granted/modified (not initial invite):**

```python
Notification.objects.create(
    user=guest_user,
    org=org,
    type=Notification.Type.SYSTEM_ALERT,
    payload={
        "action": "workflows_added",  # or "workflows_removed", "access_revoked"
        "workflow_names": ["Workflow A", "Workflow B"],
        "changed_by": admin_user.id,
    },
)
```

Rendered as:

- "You now have access to Workflow A and Workflow B in Acme Corp"
- "Your access to Workflow C in Acme Corp has been removed"
- "Your guest access to Acme Corp has been removed"

### 8) Edge Cases

**Guest upgraded to member:**

- When a user becomes an org member, clean up their guest status:
  - Deactivate all `WorkflowAccessGrant` records for that org
  - Cancel any pending `GuestInvite` or `WorkflowInvite` for that user in that org
- Members have broader access via roles, so grants are redundant

**Workflow deleted:**

- `WorkflowAccessGrant` records cascade-delete with the workflow
- No notification (workflow simply disappears)

**Workflow archived:**

- Grants remain but workflow hidden from guest's list
- If unarchived, access is restored

**Workflow visibility changed (private → public):**

- Existing grants and pending invites remain (redundant but harmless)
- No notification needed

**Workflow visibility changed (public → private):**

- Only org members and users with existing grants retain access
- Users who previously could access (because public) lose access
- No notification or warning (they weren't explicitly granted access)

**Guest has no remaining workflow access:**

- Disappears from org guest list
- Remains a user but has no access to that org's resources
- Can be re-invited later

### 9) Permissions

Guest management requires ADMIN or AUTHOR. Admins can invite to all workflows. Authors can only
invite guests to workflows they authored.

No new permission code is required. Use existing admin-or-author checks for the org-level guests
page and workflow edit permissions for per-workflow sharing. Author views must scope queries to
workflows they authored.

---

## Implementation Plan

### Phase 1: Workflow Visibility

1. Rename `make_info_public` to `make_info_page_public` (migration + reference updates)
2. Add `is_public` field to Workflow model
3. Auto-sync `make_info_page_public=True` when `is_public=True`
4. Update `can_execute()` and public list queries to use the new fields
5. Add "Sharing" tab to workflow settings with the visibility toggle

### Phase 2: Guest Invite Models + Notifications

1. Add `GuestInvite` model and acceptance flow
2. Add guest invite notification type, templates, and Notification links
3. Keep `PendingInvite` as member-only

### Phase 3: Per-Workflow Sharing UI

1. Create `WorkflowSharingView` (the Sharing tab)
2. Create `WorkflowGuestInviteView` (invite form)
3. Create `WorkflowGuestRevokeView` (remove access)
4. Wire up existing `WorkflowInvite` model and emails
5. Show guest counts on workflow list cards and table rows

### Phase 4: Org-Level Guest Management

1. Create `GuestListView`, `GuestInviteCreateView`, `GuestUpdateView`, `GuestDeleteView`
2. Create templates with HTMX interactions mirroring the Members layout
3. Add "Guests" to org settings navigation
4. Update guest invite acceptance flow for org-level invites
5. Add notification templates for access changes

### Phase 5: Edge Case Handling

1. Clean up guest grants when user becomes member
2. Handle workflow archive/delete gracefully

---

## Alternatives Considered

### A) Single visibility boolean vs enum

Could use an enum (`PRIVATE`, `ORG`, `PUBLIC`) for future flexibility.

**Decision:** Start with boolean `is_public`. Can migrate to enum later if needed.
YAGNI - we only need two levels now.

### B) Separate guest invitation model

Could create `GuestInvite` instead of extending `PendingInvite`.

**Decision:** Use a dedicated `GuestInvite` model so guest invites never touch member logic,
seat enforcement, or member notifications.

### C) Per-workflow sharing only (no org-level view)

Could skip the org-level guest management and only do per-workflow sharing.

**Decision:** Both are needed. Per-workflow is for quick sharing; org-level is for admins
to see/manage all guests across the organization.

---

## Security Considerations

- Admins and authors can manage guests within their workflow scope
- Guest invites scoped to org (can't invite to workflows in other orgs)
- Invitation tokens expire after 7 days
- All access changes auditable via `WorkflowAccessGrant.granted_by` and timestamps
- Public workflows still require authentication (no anonymous execution)
- Public workflow usage billed to owner's org (rate limits apply)
- Public workflow launches use the `workflow_launch_public` throttle scope with a
  default of `10/hour` per user (env override)
