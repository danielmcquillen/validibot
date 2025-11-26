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

### Role codes and permission checks

Role membership is defined in `users.constants.RoleCode`. Current codes are:

- `OWNER` – organizational accountability; automatically implies every other role.
- `ADMIN` – organization management actions (invite/remove members, edit org).
- `AUTHOR` – build and maintain workflows (create, edit, clone, delete steps).
- `EXECUTOR` – launch validation runs (paired with RESULTS_VIEWER when review is needed).
- `RESULTS_VIEWER` – read-only access to validation results across the org.
- `WORKFLOW_VIEWER` – read-only access to workflow definitions/metadata.

Exactly one membership per organization can hold the `OWNER` role at a time. Assigning `OWNER` to another member automatically removes it from the previous holder (they retain any remaining roles, including `ADMIN`).

The column values in the database come from `RoleCode`, which keeps the string
representation consistent everywhere we compare roles.

### Membership lifecycle

- Creating a membership immediately activates it (`is_active=True`).
- Assigning a role is done via `Membership.add_role(role_code)`. The helper makes sure the backing `Role` row exists before adding the through relation.
- Authorization should call `user.has_perm(PermissionCode.<code>.value, obj_with_org)`; the org permission backend maps roles to permissions and scopes to the object’s org. `Membership.has_role` remains available for business rules (e.g., “do not remove final OWNER”).

Roles can be added or removed through the service layer, but the low level
helpers keep the database consistent.

## Personal organizations

When a user signs in for the first time we call
`User.get_current_org()`. If they do not yet have an organization, we:

1. Create a new `Organization` flagged as `is_personal=True`.
2. Create a corresponding `Membership` for the user.
3. Grant the `ADMIN`, `OWNER`, and `EXECUTOR` roles to that membership so they can
   manage the org, invite others, and run workflows immediately (matches the behavior in `ensure_personal_workspace`).
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
3. Authorization is evaluated with `user.has_perm(PermissionCode.<code>.value, obj_with_org)`; the permission backend reads the user’s active membership and the object’s org.
4. Shared helpers (for example `grant_role` in the test factories) keep membership roles synchronized; permissions flow automatically from those roles.
5. The Workflow and Validation APIs mirror the UI guards via permission codes:
   - create/update/delete workflow: `workflow_edit`
   - start workflow run: `workflow_launch`
   - view validation results: `results_view_all` or `results_view_own`
   - manage org/users: `admin_manage_org`

## How roles, permissions, and (future) Django Groups line up

We keep the data model simple and Django-native:

- **Tables:** `User` ←→ `Membership` ←→ `Organization`, with `MembershipRole` joining to `Role` rows keyed by `RoleCode`.
- **Permission codes:** Defined in `users.constants.PermissionCode` and seeded via migration `users/migrations/0005_permission_definitions.py`.
- **Backend:** `OrgPermissionBackend` implements Django’s `has_perm` contract and translates a user’s roles-in-org into permission grants on a per-object basis. It also handles “own” semantics (`results_view_own` checks run.user_id).
- **Groups:** We do **not** create or rely on Django `Group` objects today. If you need them (e.g., for Django admin or external tooling), you can mirror `Role` → `Group` mappings without changing authorization call sites because everything already uses `user.has_perm`.

### What happens when a user joins an org?

1. An `Organization` exists (personal orgs are created automatically on first login).
2. A `Membership` row is created (`is_active=True`).
3. The inviter or form assigns one or more `Role` codes (e.g., `EXECUTOR`, `RESULTS_VIEWER`).
4. From that point on, `user.has_perm("workflow_launch", workflow)` and `user.has_perm("results_view_all", run)` will return `True` when the object’s `org` matches the membership because the backend maps those roles to permission codes.

### When roles change in the UI

- The member roles form updates the `MembershipRole` set.
- No additional permission bookkeeping is required—the next `has_perm` call reflects the new roles.
- Integrity rules (e.g., “cannot remove the last OWNER/ADMIN”) are enforced with role-aware checks, but all request authorization uses `has_perm`.

### Concrete `has_perm` examples

```python
# Workflow access
user.has_perm(PermissionCode.WORKFLOW_VIEW.value, workflow)
user.has_perm(PermissionCode.WORKFLOW_EDIT.value, workflow)
user.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, workflow)

# Validation runs
user.has_perm(PermissionCode.RESULTS_VIEW_ALL.value, validation_run)
user.has_perm(PermissionCode.RESULTS_VIEW_OWN.value, validation_run)  # True for run owner

# Org admin / member management
user.has_perm(PermissionCode.ADMIN_MANAGE_ORG.value, organization)
```

### API and UI alignment

- DRF permissions and view mixins call `has_perm` with the target object (workflow, validation run, or organization) so org scoping is automatic.
- Templates rely on precomputed flags that were derived from `has_perm`; avoid checking roles directly in templates to keep behavior consistent.

### Extending with Django Groups (if ever needed)

- Create a `Group` per `RoleCode` (or per `PermissionCode`) and assign the seeded `auth.Permission` rows to that group.
- When adding a membership role, also add the user to the corresponding group. Authorization continues to use `has_perm`, so this is transparent to the rest of the codebase.
- This pattern makes Django admin and third-party tooling work without touching the authorization layer in application code.

### Tips

- **Always** scope queries to `request.user.get_current_org()` or an explicit
  org from the URL to avoid leaking cross-org data.
- Prefer the helpers on `Membership` instead of touching `MembershipRole`
  directly.
- When inviting a user to an organization, add all required roles in one place.
- The `Role` table is intentionally short; feel free to extend it with display
  names or descriptions to power better UI messaging.
- Remember that organization management views look specifically for the `ADMIN`
  role (`OrganizationAdminRequiredMixin`), and the Owner role now automatically
  grants it (along with every other role) so owners always meet those checks.
- If you promote someone else to Owner, the system will automatically drop the role from the previous Owner so the uniqueness guarantee holds.
