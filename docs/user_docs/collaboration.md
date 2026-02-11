# Collaboration and Access

Validibot is designed for teams. This guide explains how to manage access, invite teammates, and share validation work across your organization.

## Understanding Roles

Every member of an organization has a role that determines what they can do. Roles are hierarchical—higher roles include all permissions of lower roles.

### Role Overview

| Role | Can Do | Best For |
|------|--------|----------|
| **Owner** | Everything, including deleting the org | Organization creators, primary administrators |
| **Admin** | Manage members, all workflow operations | Team leads, department heads |
| **Author** | Create and edit workflows, run validations | Engineers, analysts who build workflows |
| **Executor** | Run validations, view results | Team members who submit data |
| **Viewer** | View workflows and results (read-only) | Stakeholders, auditors |

### Role Details

**Owner**: Full control over the organization. Can delete the organization, manage billing (Pro edition), and perform all administrative tasks. Every organization must have at least one owner.

**Admin**: Can invite and remove members, assign roles (except Owner), and manage all workflows regardless of who created them. Cannot delete the organization or manage billing.

**Author**: Can create new workflows, edit workflows they created, and manage validators. Can also run validations and view all results. Authors cannot edit workflows created by others (Admins can).

**Executor**: Can launch validation runs on any active workflow and view results. Cannot create or modify workflows. This role is ideal for team members who submit data for validation but don't need to configure the validation logic.

**Viewer**: Read-only access to workflows and validation results. Useful for stakeholders who need to review outcomes but shouldn't make changes.

## Inviting Teammates

To add someone to your organization:

1. Navigate to **Settings** → **Members** (or **Team**, depending on your UI)
2. Click **Invite Member**
3. Enter their email address
4. Select the role you want to assign
5. Click **Send Invitation**

The invitee receives an email with a link to join. If they don't have a Validibot account, they'll be prompted to create one.

### Invitation Best Practices

**Start with appropriate access**: Assign the minimum role needed. You can always upgrade later.

**Use descriptive messages**: When inviting, add context about what you'd like them to do.

**Follow up**: If someone hasn't accepted within a few days, they may have missed the email. Resend or reach out directly.

### Pending Invitations

You can view pending invitations in the Members settings. From there you can:

- Resend the invitation email
- Cancel invitations that are no longer needed
- See when invitations were sent

## Managing Members

### Changing Roles

Admins and Owners can change member roles:

1. Go to **Settings** → **Members**
2. Find the member you want to update
3. Click the role dropdown and select a new role
4. Confirm the change

Role changes take effect immediately.

### Removing Members

To remove someone from the organization:

1. Go to **Settings** → **Members**
2. Find the member to remove
3. Click **Remove** (or the trash icon)
4. Confirm the removal

Removed members lose access immediately. Their past actions (workflows created, runs submitted) remain in the system but are attributed to "[Removed User]" or similar.

### Transferring Ownership

To transfer organization ownership:

1. The current Owner adds the new person as an Admin (if not already)
2. The current Owner promotes them to Owner
3. The previous Owner can then be demoted or removed

An organization can have multiple Owners, but consider keeping the list small for security.

## Sharing Workflows and Results

### Within Your Organization

All organization members can see workflows and runs based on their role:

- **Viewers** and above can see all workflows and results
- **Executors** and above can run validations
- **Authors** and above can create and modify workflows

There's no need to explicitly share within your organization—access is automatic based on roles.

### Sharing Run Links

To share a specific validation run:

1. Open the run detail page
2. Copy the URL from your browser
3. Send it to anyone with organization access

The recipient must have at least Viewer access to see the run.

### Public Information

Some workflows have a "public information" field that describes what the workflow does. This description may be visible to users outside your organization (depending on configuration). However, actual validation results and findings are always restricted to organization members.

## Working Across Projects

Projects help organize workflows within an organization. When you create or move a workflow to a project:

- The workflow inherits the organization's access controls
- Project membership doesn't grant additional permissions
- All organization members can see all projects (based on their role)

Projects are organizational, not security boundaries. Use organizations when you need true access separation.

## Notifications

Validibot can notify you about important events. Notification channels include:

**In-app notifications**: Appear in the notification bell in the header

**Email**: For important events like failed runs or completed long-running validations

**Workflow actions**: Workflows can include notification steps (Slack messages, webhooks) that trigger based on validation outcomes

### Configuring Notifications

To adjust your notification preferences:

1. Click your avatar → **Settings** (or **Profile**)
2. Find the **Notifications** section
3. Toggle which events you want to be notified about
4. Save your preferences

### Workflow-Level Notifications

Workflow authors can add notification steps that trigger automatically:

- **On success**: Notify a channel when validation passes
- **On failure**: Alert the team when validation fails
- **Always**: Send a notification regardless of outcome

These are configured in the workflow editor, not in personal settings.

## Best Practices

**Principle of least privilege**: Give people the minimum access they need. It's easy to upgrade roles later.

**Regular access reviews**: Periodically review who has access, especially for Admin and Owner roles.

**Document your structure**: Keep notes on what each project is for and who should have access to what.

**Use Executors for automation**: Service accounts or CI/CD integrations typically only need Executor access to submit runs.

**Separate environments**: If you need true separation (e.g., production vs. staging, different clients), use separate organizations rather than relying on projects.
