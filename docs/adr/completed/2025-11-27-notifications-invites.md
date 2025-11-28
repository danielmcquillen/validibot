# ADR: Add Notification Model and In‑App Invite Handling

**Status:** Proposed  
**Date:** 2025‑11‑27

## Context

- Invites are sent via email, and invitees must use the email link to accept. There is no in‑app surface to see pending invites or act on them.
- Users can miss or lose email links; we also need a home for future alerts (e.g., workflow run failures) without building realtime push yet.
- We want a lightweight, Django/HTMX-friendly solution, scoped to the active organization and the current user.
- We want a privacy-preserving invite flow similar to GitHub: type-ahead search that shows a few matches (username/full name/email) without exposing a full user list; fallback to raw email when no match is found; invites expire and can be canceled/rescinded; invitees see an in-app notification to accept/decline.

## Decision

Introduce a notifications page and a `Notification` model; support invite notifications as the first actionable type, and add a GitHub-style invite flow with type-ahead search and a tabbed view for current invites.

- Add `/notifications/` to list unread (and optionally read) notifications for the current user in the active org.
- Add HTMX endpoints for invite notifications:
  - `notifications/invite/<uuid:pk>/accept/`
  - `notifications/invite/<uuid:pk>/decline/`
- Accept/decline will verify ownership, ensure validity, and on accept create/activate a `Membership` with the proposed roles; on decline remove/mark the invite as declined. Responses return a partial to replace the row inline. The inviter’s view should reflect the new status.
- Future notification types can reuse the same page and partials.
- Invite creation flow adopts GitHub’s type-ahead search: admins type at least a few characters (e.g., 3) of username/full name/email; the backend returns a short list of matches (name + avatar) without exposing a full directory. If no match, admins can invite via raw email.
- Invites become `PendingInvite` records with org, inviter, invitee (user or email), proposed roles, expiry (e.g., 7 days), and status (pending/accepted/declined/canceled). Sending an invite also creates a `Notification(type="invite")` for the invitee.

For the inviter (Add member card):

- Replace the single panel with two tabs: **Invite member** and **Current invitations**.
- Invite member tab: type-ahead search input + role selection (Owner/Admin/Author/Executor/Analytics Viewer/Validation Results Viewer/Workflow Viewer). Allow fallback to the typed email if no user match. On submit, create a PendingInvite and send an email.
- Current invitations tab:
  - List all invites (pending, accepted, declined) with target user/email, roles, date sent, and date accepted/declined.
  - Allow cancel on pending invites; accepted/declined are read-only.

## Routing

- `path("notifications/", NotificationListView.as_view(), name="notification-list")`
- `path("notifications/invite/<uuid:pk>/accept/", AcceptInviteView.as_view(), name="notification-invite-accept")`
- `path("notifications/invite/<uuid:pk>/decline/", DeclineInviteView.as_view(), name="notification-invite-decline")`
- `path("invites/search/", InviteSearchView.as_view(), name="invite-search")` (HTMX/JSON for type-ahead)
- `path("invites/create/", InviteCreateView.as_view(), name="invite-create")`
- `path("invites/cancel/<uuid:pk>/", InviteCancelView.as_view(), name="invite-cancel")`

## Views/Templates

- `NotificationListView`: scoped to `request.user` + active org; `get_queryset` filters and orders by `created_at` desc.
- Partials:
  - `notifications/partials/invite_row.html`: shows inviter, org, roles, Accept/Decline buttons (`hx-post` to accept/decline URLs; `hx-target` row).
  - `notifications/notification_list.html`: loops notifications, includes rows; may separate unread/read.
- Invite form/tab: type-ahead search input with dropdown matches; if no match, allow sending to the raw email. After selection, show role checkboxes (Owner/Admin/Author/Executor/Analytics Viewer/Validation Results Viewer/Workflow Viewer) with sensible defaults.
- Current invitations tab: list of pending/accepted/declined invites with status, dates, and a cancel button for pending ones.
- Accept/Decline views return a small snippet acknowledging the action (e.g., “Joined Acme” or remove row).

## Model

New `Notification` model:

- `id` (UUID primary key)
- `user` (FK User)
- `org` (FK Organization)
- `type` (choices: e.g., `invite`, `system_alert`)
- `payload` (JSONField for type-specific data: inviter, roles, invite UUID)
- `created_at`, `read_at`

`PendingInvite` (new or extend existing invite model):

- `id` (UUID)
- `org` (FK Organization)
- `inviter` (FK User)
- `invitee_user` (FK User, nullable) and/or `invitee_email`
- `roles` (JSON/Text field capturing proposed roles)
- `status` (pending/accepted/declined/canceled/expired)
- `expires_at`
- Timestamps (created_at, updated_at)

Invite linkage:

- When sending an invite, create a `Notification(type="invite", invite=<FK>)` with payload pointing to the invite and proposed roles. Use a nullable FK to `PendingInvite` so we retain referential integrity for invite notifications while keeping Notification generic.
- Mark notification read on view/action; read notifications can be hidden or shown under a “Read” divider.
- On accept: mark invite accepted, create/activate Membership with roles, clear the notification row with a success message, and update the inviter’s “Current Invitations” tab.
- On decline or cancel: mark invite declined/canceled, remove/mark the notification, and reflect status in the inviter tab.
- On accept or decline, also create a notification for the inviter so they are explicitly informed of the outcome (e.g., “User X accepted your invite to Org Y”).
- Expiry enforcement: use lazy expiry. Any time invites are read or acted on (listing invites, rendering the notifications page, accept/decline/cancel actions), check `expires_at` and flip status to `expired` as needed. Expired invites cannot be accepted; notify the user and inviter accordingly. This avoids a hard dependency on cron/scheduled jobs for MVP.
- Tracking/audit: log invite lifecycle events to our tracker (inviter, invitee, org, roles, timestamp, status) for:
  - Invite created (with target user/email, roles, expires_at)
  - Invite accepted (membership created/activated)
  - Invite declined/canceled/expired
  - Notification created/read for both invitee and inviter
  Include minimal identifiers (invite UUID, org ID, inviter/invitee user IDs) and the new status.

## UX

- Dedicated page in nav (badge for unread count is optional now).
- Inline HTMX actions; no full page reload on accept/decline.
- Scoped to active org; only the invitee sees their invites.
- Invite form uses GitHub-style type-ahead search (no full user list). Show a small dropdown of matches; if no match, allow inviting the typed email.
- Invitee side: pending invites surface on the notifications page and (optionally) a small bell/badge in the nav; accept/decline inline.
- Navigation surfacing: add a small bell icon with unread count in the header, to the left of the user profile dropdown. Clicking the bell goes directly to `/notifications/`. Also include a “Notifications” link in the profile dropdown for redundancy.

## Security/Permissions

- Accept/Decline must verify:
  - Notification belongs to `request.user` and matches the active org.
  - Invite is valid and target matches the user/email.
- Only admins can send invites and assign roles (existing constraint).
- No cross-org leakage in list or actions.
- Invite search: enforce minimum query length (e.g., 3 chars), throttle, and return limited fields (name/avatar). Do not expose email addresses in search results unless inviting by that email explicitly.
- Email verification: if inviting by email, require that the account email is verified before acceptance.
- Expiration and revocation: set expiration (e.g., 7 days) and allow cancel/resend.

## Testing

- Creation: invites produce notifications.
- Listing: only current user + active org.
- Accept/Decline: updates membership (accept) or removes/marks notification (decline); HTMX fragment correctness.
- Optional: unread badge count if added.
- Invite search: respects min query length, throttling, privacy (no overbroad results).
- Invite lifecycle: pending→accepted/declined/canceled/expired updates status, membership creation, notification state, and inviter view.

## Alternatives

- Email-only: rejected (missed/expired links, no in-app visibility).
- Realtime push/websockets: deferred; more infra/complexity than needed for MVP.

## App placement

- Invite and membership logic should live in the **users** app alongside organizations, memberships, and roles, because invites are a precursor to memberships.
- Notifications infrastructure can live in a small **notifications** app if we want to decouple notification plumbing, but invite creation/acceptance should remain in the users app and emit notifications via that shared service.

## Future Work

- Additional notification types (workflow completion, comments, system maintenance).
- Nav dropdown with unread count.
- NotificationService to centralize creation logic.
- Reuse the model/views if/when adding websockets.
