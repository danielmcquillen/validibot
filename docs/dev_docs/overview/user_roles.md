# User Roles and Permissions

SimpleValidations implements a comprehensive role-based access control (RBAC) system that governs how users can interact with workflows, submissions, and validation runs within their organizations.

## Overview

The permission system is built around three core concepts:

1. **Organizations**: Top-level containers that provide isolation and context
2. **Memberships**: The relationship between users and organizations
3. **Roles**: Sets of permissions that define what actions users can perform

Users can belong to multiple organizations with different roles in each, providing flexible access management across teams and projects.

## Role Hierarchy

SimpleValidations defines four primary roles, listed from highest to lowest privilege:

### Owner (OWNER)

**Full administrative control over the organization**

**Permissions:**

- All Author, Executor, and Viewer permissions
- Manage organization settings and billing
- Invite and remove users from the organization
- Assign and revoke roles for other users
- Delete the organization (where applicable)
- Access to all workflows, regardless of specific workflow permissions

**Typical Users:**

- Organization founders
- Department heads
- Primary administrators

**Notes:**

- Every organization must have at least one Owner
- Owners are automatically created when new organizations are established
- Personal workspaces automatically assign Owner role to the user

### Author (AUTHOR)

**Create and manage validation workflows**

**Permissions:**

- All Executor and Viewer permissions
- Create new workflows within the organization
- Edit existing workflows (name, steps, configuration)
- Delete workflows they created
- Manage workflow-specific access permissions
- Upload and manage rulesets (validation schemas)
- View organization usage statistics

**Typical Users:**

- Data architects
- Validation engineers
- Team leads responsible for data quality standards

**Notes:**

- Authors can only modify workflows within their organization
- They cannot change organization-level settings or manage user memberships

### Executor (EXECUTOR)

**Execute validations and view results**

**Permissions:**

- All Viewer permissions
- Start validation runs against accessible workflows
- Upload submissions for validation
- Access detailed validation results and logs
- Download validation artifacts and reports
- Re-run existing validations

**Typical Users:**

- Application developers
- Data engineers
- QA engineers
- Anyone who needs to validate data regularly

**Notes:**

- This is the minimum role required to actually perform validations
- Executors cannot modify workflows but can use them extensively
- API integrations typically use accounts with Executor role

### Viewer (VIEWER)

**Read-only access to validation information**

**Permissions:**

- Browse workflows they have access to
- View workflow configurations and steps
- See validation run history and status
- View high-level validation results
- Access organization and workflow documentation

**Typical Users:**

- Stakeholders who need visibility into data quality
- Auditors and compliance personnel
- New team members during onboarding
- External consultants with limited access needs

**Notes:**

- Cannot execute validations or make any changes
- Useful for providing transparency without operational access
- Default role for new organization members

## Organization Model

### Organization Types

**Regular Organizations**

- Multi-user workspaces for teams and companies
- Managed by Owners who can invite other users
- Support all role types and collaborative workflows
- Can have custom billing and usage limits

**Personal Organizations**

- Single-user workspaces automatically created for each user
- User is automatically assigned Owner role
- Provides a private space for personal validation work
- Can be used for prototyping before sharing with teams

### Membership Management

Users join organizations through:

1. **Invitation**: Existing Owners invite users via email
2. **Automatic Creation**: Personal organizations are created automatically
3. **Self-Service**: Organizations can enable open registration (optional)

Each membership tracks:

- **Join Date**: When the user became a member
- **Active Status**: Whether the membership is currently active
- **Role History**: Audit trail of role changes over time

## Workflow Access Control

Beyond organization-level roles, SimpleValidations provides workflow-specific access control:

### Workflow Visibility

By default, workflows inherit organization-level permissions:

- **Owners**: See all workflows in the organization
- **Authors**: See all workflows in the organization
- **Executors**: See workflows they have access to execute
- **Viewers**: See workflows they have permission to view

### Fine-Grained Workflow Permissions

Authors can configure additional access restrictions:

**Role-Based Workflow Access**

- Restrict workflow access to specific roles within the organization
- Example: Limit sensitive financial validation workflows to Owners only
- Implemented via `WorkflowRoleAccess` model

**Future Enhancements** (Planned)

- User-specific workflow permissions
- Project-based access control
- Time-limited access grants

## Permission Enforcement

### API Level

All REST API endpoints enforce authentication and role-based permissions:

```python
# Example: Starting a validation requires EXECUTOR role
def can_execute(self, *, user: User) -> bool:
    """Check if user can execute this workflow."""
    return Workflow.objects.for_user(
        user,
        required_role_code=RoleCode.EXECUTOR
    ).filter(pk=self.pk).exists()
```

### Database Level

Queries are automatically filtered based on user permissions:

```python
# Users only see workflows they have access to
def get_queryset(self):
    return Workflow.objects.for_user(self.request.user)
```

### UI Level

Templates and forms adapt based on user roles:

- Owners see organization management options
- Authors see workflow creation buttons
- Executors see validation execution controls
- Viewers see read-only interfaces

## Security Features

### Multi-Tenancy Isolation

- Organizations provide complete data isolation
- Users cannot access resources outside their member organizations
- Queries are automatically scoped to prevent data leakage

### Audit Logging

- All role changes are logged with timestamps and actors
- Workflow access attempts are tracked
- Failed permission checks generate security events

### Session Management

- Users can switch between organizations they belong to
- Current organization context affects all operations
- Session state tracks active organization for security

## Common Use Cases

### Enterprise Team Structure

```
Organization: "Acme Corp Data Team"
├── Owner: Alice (Head of Data)
├── Authors: Bob (Senior Engineer), Carol (Data Architect)
├── Executors: Dave (Developer), Eve (QA Engineer)
└── Viewers: Frank (Product Manager), Grace (Compliance)
```

### Multi-Project Environment

```
User: John Smith
├── Personal Org: "John's Workspace" (Owner)
├── Work Org: "Tech Corp" (Executor)
└── Client Org: "Customer Inc" (Viewer)
```

### API Integration Pattern

```
Service Account: "production-validator"
└── Role: Executor in "Production Data Org"
    └── Purpose: Automated validation in CI/CD pipeline
```

## Role Assignment Best Practices

### Principle of Least Privilege

- Start with Viewer role for new users
- Promote to higher roles as responsibilities increase
- Regularly audit role assignments for appropriateness

### Separation of Duties

- Authors design workflows but may not execute them in production
- Executors run validations but cannot modify workflow logic
- Viewers provide oversight without operational access

### Emergency Access

- Maintain multiple Owners per organization
- Document emergency access procedures
- Consider temporary role escalation for incident response

## Future Enhancements

### Planned Features

- **Custom Roles**: Organization-defined roles with granular permissions
- **Conditional Access**: Time-based and IP-based access restrictions
- **Project-Level Permissions**: Sub-organization permission scoping
- **Advanced Audit**: Enhanced logging and compliance reporting
- **Single Sign-On**: Integration with enterprise identity providers
- **API Keys**: Service account management with scoped permissions

### Integration Points

- **External Identity Providers**: SAML, OAuth, LDAP integration
- **Workflow Automation**: Role-based workflow triggers and notifications
- **Compliance Frameworks**: SOC 2, GDPR, HIPAA compliance features

## Troubleshooting Common Issues

### "Permission Denied" Errors

1. Verify user has active membership in the organization
2. Check if user has the required role for the operation
3. Confirm the workflow allows access for the user's role
4. Ensure user is operating in the correct organization context

### Role Assignment Problems

1. Only Owners can assign roles to other users
2. Users cannot assign roles higher than their own
3. Every organization must maintain at least one Owner
4. Role changes may require session refresh to take effect

### Access Control Debugging

```python
# Check user's roles in an organization
membership = user.membership_for_current_org()
roles = membership.roles.all() if membership else []

# Verify workflow access
can_access = workflow.can_execute(user=user)

# Debug organization membership
orgs_with_roles = [
    (membership.org.name, list(membership.roles.values_list('code', flat=True)))
    for membership in user.memberships.filter(is_active=True)
]
```

This role-based access control system provides SimpleValidations with enterprise-grade security while maintaining the flexibility needed for diverse organizational structures and workflows.
