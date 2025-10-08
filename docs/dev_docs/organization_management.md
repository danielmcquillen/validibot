# Organization & Project Management

This guide summarises the new administrative tooling introduced for
organization owners. Only members with the `ADMIN` role can create or delete
organizations, manage membership, or edit projects. Regular members continue
to use the organization picker to switch context, but do not see the
management links.

## Organization Picker

- The picker lives at the top of the application sidebar.
- It displays the currently scoped organization and supports switching by
  posting to `users:organization-switch`.
- Switching updates both the session (`active_org_id`) and the user’s
  `current_org` field so the selection persists across requests (subdomains can
  later be layered on top).

## Organization CRUD

- **List view** (`users:organization-list`) shows every org where the current
  user is an admin and provides shortcuts to edit or review membership.
- **Create** (`users:organization-create`) automatically assigns the creator
  the `ADMIN`, `OWNER`, and `EXECUTOR` roles and scopes the session to the new org.
- **Edit** (`users:organization-update`) allows renaming.
- **Delete** (`users:organization-delete`) requires at least one other admin
  before removal, prohibits deleting personal workspaces, and re-scopes the session
  to the next available org.

## Membership Management

- **Detail view** (`users:organization-detail`) lists active members.
- Admins can invite an existing user by email and select initial roles.
- Updating roles or removing a member prevents the final admin from being
  demoted or removed, safeguarding against orphaned organizations.

## Projects

- **List/Create/Edit/Delete** views live under the `projects` app and are
  restricted to admins of the active organization.
- Projects are soft-deleted; default projects (created automatically for every
  organization) cannot be removed. HTMX actions keep the list responsive.
- Deleting a project detaches submissions, validation runs, and tracking events
  from the project so historical data remains intact.

## Navigation Changes

- The left sidebar now includes the organization picker and admin-only links to
  “Manage organizations” and “Manage projects”.
- Non-admins only see the picker and their standard navigation items.

See the tests under `simplevalidations/users/tests/test_organization_management.py`
and `simplevalidations/projects/tests/test_project_management.py` for examples of
expected behaviour and permission boundaries.
