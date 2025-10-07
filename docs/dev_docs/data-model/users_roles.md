# Users, Organizations, and Roles

SimpleValidations is a multi-tenant system. Every resource is scoped to an
**Organization**, and every authenticated action runs in the context of the
organization the user currently has selected. This document captures how the
user/organization relationship is modeled and enforced.

## Core Tables

| Model | Purpose |
| --- | --- |
| `users.User` | Application account. Holds profile information, current organization pointer, and helper methods for org membership. |
| `users.Organization` | Tenant boundary. May be marked `is_personal` when created just for a single user. |
| `users.Membership` | Through table joining `User` and `Organization`. Adds `is_active` so a user can be invited/suspended without losing history. |
| `users.Role` | Catalog of role codes. Codes mirror `RoleCode` values so we can attach descriptions or rename without touching code. |
| `users.MembershipRole` | Through table between `Membership` and `Role`, allowing a user to hold multiple roles inside the same org. |

### Role codes

Role membership is defined in `users.constants.RoleCode`. The important ones for
workflow execution today are:

- `OWNER` – full administrative control, including managing roles and billing.
- `ADMIN` – day-to-day management powers.
- `EXECUTOR` – ability to launch workflows and see validation results.
- `VIEWER` – read-only visibility.

The column values in the database come from `RoleCode`, which keeps the string
representation consistent everywhere we compare roles.

### Membership lifecycle

- Creating a membership immediately activates it (`is_active=True`).
- Assigning a role is done via `Membership.add_role(role_code)`. The helper makes
  sure the backing `Role` row exists before adding the through relation.
- Checking a role uses `Membership.has_role(role_code)` which performs a single
  `EXISTS` query.

Roles can be added or removed through the service layer, but the low level
helpers keep the database consistent.

## Personal organizations

When a user signs in for the first time we call
`User.get_current_org()`. If they do not yet have an organization, we:

1. Create a new `Organization` flagged as `is_personal=True`.
2. Create a corresponding `Membership` for the user.
3. Grant both `OWNER` _and_ `EXECUTOR` roles to that membership so they can
   manage the org and run workflows immediately.
4. Persist the new org as `user.current_org`.

Granting the executor role automatically was recently hardened so personal orgs
behave the same as manually-invited ones.

## Managing the active organization

The active org controls which resources the user sees. `User.set_current_org()`
validates that the user has an active membership before updating the pointer.
`User.membership_for_current_org()` returns the membership record so views can
inspect roles without additional queries.

## Access control flow

1. View/service pulls the user’s `current_org` (or the explicit org in the
   request).
2. Membership is fetched using `membership_for_current_org()`.
3. Role checks are evaluated using `has_role(RoleCode.EXECUTOR)` etc.
4. Shared helpers (for example `grant_role` in the test factories) use the same
   primitives so business logic stays consistent.

### Tips

- **Always** scope queries to `request.user.get_current_org()` or an explicit
  org from the URL to avoid leaking cross-org data.
- Prefer the helpers on `Membership` instead of touching `MembershipRole`
  directly.
- When inviting a user to an organization, add all required roles in one place.
- The `Role` table is intentionally short; feel free to extend it with display
  names or descriptions to power better UI messaging.
