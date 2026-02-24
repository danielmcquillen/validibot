# Users and Organizations

Validibot is a multi-tenant system. Every resource is scoped to an **Organization**, and every authenticated action runs in the context of the user's current organization.

## Community edition setup

In the community edition, each user gets a personal organization created automatically on first login. This organization is flagged `is_personal=True`. The user is granted `OWNER`, `ADMIN`, and `EXECUTOR` roles on their personal org, giving them full access to all features.

Community users don't need to think about roles or permissions -- everything is accessible by default. The roles infrastructure exists in the codebase because it's shared with the Pro edition's team management features, but in a single-user setup it's invisible.

## Core tables

| Model | Purpose |
|-------|---------|
| `users.User` | Application account. Holds profile information and current organization pointer. |
| `users.Organization` | Tenant boundary. May be `is_personal=True` for single-user orgs. |
| `users.Membership` | Joins User to Organization. Includes `is_active` for invite/suspend without losing history. |
| `users.Role` | Catalog of role codes (e.g. `OWNER`, `ADMIN`, `AUTHOR`). |
| `users.MembershipRole` | Through table between Membership and Role, allowing multiple roles per user per org. |

## Personal organizations

When a user signs in for the first time and has no organization:

1. A new `Organization` is created with `is_personal=True`.
2. A `Membership` is created for the user.
3. The `OWNER`, `ADMIN`, and `EXECUTOR` roles are granted.
4. The org is set as the user's `current_org`.

## Active organization

The active org controls which resources the user sees. `User.set_current_org()` validates that the user has an active membership before updating the pointer. Views use `User.get_current_org()` to scope all queries.

## Authorization

Authorization uses Django's `has_perm` contract. The `OrgPermissionBackend` maps a user's roles to permission codes and scopes them to the object's organization:

```python
user.has_perm(PermissionCode.WORKFLOW_EDIT.value, workflow)
user.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, workflow)
```

Always scope queries to the user's current org to avoid leaking cross-org data.

## Role details

For full documentation on role codes, the permission hierarchy, the role picker UI, and team management workflows, see [User Roles](../overview/user_roles.md).
