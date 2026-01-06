# ADR: CLI and API Support

**Date:** 2025-12-22
**Status:** Partially superseded by [ADR-2026-01-06](completed/2026-01-06-org-scoped-routing-and-versioned-workflow-identifiers.md)
**Context:** Implementing a public-facing API and CLI for programmatic access to Validibot

> **Note:** The API routing described in this ADR has been replaced by org-scoped routes. The new canonical endpoint pattern is `/api/v1/orgs/{org_slug}/workflows/{workflow_identifier}/runs/` instead of `/api/v1/workflows/{id}/start/`. See ADR-2026-01-06 for details.

## Summary

This ADR documents the design decisions for supporting a command-line interface (CLI) and minimal public API for Validibot. The goal is to enable automation and CI/CD integration while maintaining security through a conservative API surface.

## Background

Validibot needs to support two primary programmatic access patterns:

1. **CLI Access**: Users running `validibot validate model.idf --workflow <id>` from the command line
2. **API Access**: Direct HTTP calls for custom integrations

Both patterns require authentication, workflow discovery, validation launching, and result retrieval.

## Design Principles

1. **Minimal API Surface**: Only expose endpoints strictly necessary for the documented use cases
2. **Read-Heavy, Write-Light**: Users can view workflows and results but cannot modify them via API
3. **Bearer Authentication**: Use pre-generated API keys with Bearer token authentication
4. **Slug-Based Lookups**: Support human-friendly workflow identifiers where possible
5. **Consistent Terminology**: Use "API key" in user-facing contexts, "token" internally

## Implemented Changes

### 1. Authentication Endpoint (`/api/v1/auth/me/`)

**Purpose**: Validate API keys and retrieve basic user information.

**Implementation**: A minimal endpoint that returns only email and display name:

```python
# validibot/core/api/auth_views.py
class AuthMeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "email": request.user.email,
            "name": request.user.name or "",
        })
```

**Design Decisions**:

- Returns 403 Forbidden for invalid/missing tokens (DRF default behavior)
- Intentionally minimal response - no user IDs, organization info, or sensitive data
- Used by CLI during `validibot login` to verify API key validity

### 2. Bearer Token Authentication

**Implementation**: DRF's `TokenAuthentication` with "Bearer" keyword instead of "Token".

```python
# validibot/core/api/authentication.py
class BearerAuthentication(TokenAuthentication):
    keyword = "Bearer"
```

**Rationale**: "Bearer" is the OAuth 2.0 standard and what developers expect.

### 3. CLI Project Structure

The CLI is a separate Python package (`validibot-cli`) that provides:

- `validibot login` - Authenticate with API key (stored in system keyring)
- `validibot logout` - Remove stored credentials
- `validibot whoami` - Show current user info
- `validibot auth status` - Check authentication status
- `validibot workflows list` - List accessible workflows
- `validibot workflows show <id>` - Show workflow details
- `validibot validate <file> --workflow <id>` - Run validation
- `validibot validate status <run-id>` - Check validation status

**Key Files**:

- `validibot_cli/client.py` - HTTP client with Bearer auth
- `validibot_cli/auth.py` - Keyring-based credential storage
- `validibot_cli/commands/` - Typer-based command implementations

### 4. Terminology Standardization

**User-facing text** uses "API key":

- "Enter your API key"
- "API key saved successfully"
- "Get your API key from..."

**Internal code** uses "token":

- Variable names: `token`, `api_token`
- Function names: `get_stored_token()`, `save_token()`
- HTTP header: `Authorization: Bearer <token>`

This follows industry convention where "API key" describes what users generate and manage, while "token" describes the technical implementation.

### 5. API Surface Restrictions

**Goal**: Limit the API to read-only operations plus validation launching.

**API ViewSets**:

| ViewSet | Actions |
|---------|---------|
| `UserViewSet` | `me` only (removed list, retrieve, update) |
| `WorkflowViewSet` | `list`, `retrieve`, `start_validation` (read-only + start) |
| `ValidationRunViewSet` | `list`, `retrieve` (read-only, unchanged) |

**Implementation**:

1. **UserViewSet**: Changed from `mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, GenericViewSet` to just `GenericViewSet` with a `me` action.

2. **WorkflowViewSet**: Changed from `ModelViewSet` to `ReadOnlyModelViewSet` with the `start_validation` action preserved.

3. **ValidationRunViewSet**: Already read-only, no changes needed.

### 6. Slug-Based Workflow Lookup

**Problem**: Workflow slugs are unique within (org, version), not globally. Users need a way to disambiguate.

**Current Uniqueness Constraints**:

```python
# Organization: globally unique
slug = models.SlugField(unique=True)

# Project: unique within org
class Meta:
    unique_together = [("org", "slug")]

# Workflow: unique within org + version
class Meta:
    constraints = [
        UniqueConstraint(
            fields=["org", "slug", "version"],
            name="uq_workflow_org_slug_version",
        )
    ]
```

**Solution**: Add optional query parameters for disambiguation and filtering:

```
GET /api/v1/workflows/<slug>/
GET /api/v1/workflows/<slug>/?org=<org-slug>
GET /api/v1/workflows/<slug>/?org=<org-slug>&project=<project-slug>
GET /api/v1/workflows/<slug>/?org=<org-slug>&version=<version>
```

**CLI Changes**:

```bash
# Lookup by slug (fails if ambiguous)
validibot validate model.idf -w my-workflow

# Disambiguate by organization
validibot validate model.idf -w my-workflow --org my-org

# Filter by project within an organization
validibot validate model.idf -w my-workflow --org my-org --project my-project

# Specify a particular version
validibot validate model.idf -w my-workflow --org my-org --version 2
```

**Error Handling**:

- If multiple workflows match, return 400 with a list of matching (org, version) pairs
- CLI displays human-friendly message suggesting `--org`, `--project`, or `--version` options

### 7. Serializer Field Restrictions

**WorkflowSerializer** now exposes:

```python
fields = ["id", "uuid", "slug", "name", "version", "org_slug", "is_active", "allowed_file_types"]
```

**Changes made**:

- Removed `user` field (not needed by CLI/API consumers)
- Added `org_slug` as a read-only derived field (via `SlugRelatedField`)
- Removed `org` (replaced with `org_slug` for cleaner API)

## Security Considerations

1. **Rate Limiting**: The `start_validation` action is throttled to 60 requests/minute
2. **Permission Checks**: All ViewSets require `IsAuthenticated`
3. **Role-Based Access**: ValidationRuns are filtered by user permissions (own runs vs all runs)
4. **No Write Operations**: API cannot modify workflows, validators, or organization settings

## Consequences

**Positive**:

- Users can integrate Validibot into CI/CD pipelines
- API surface is minimal, reducing attack surface
- Consistent authentication pattern with industry standards

**Negative**:

- CLI users must generate API keys through the web UI
- Workflow modification requires web UI access
- Disambiguation via `--org` adds complexity for multi-org users

## Related

- [CLI README](../../../validibot-cli/README.md)
- [API Authentication Documentation](../dev_docs/api/authentication.md)
- [ADR-2025-11-27: Idempotency Keys](completed/2025-11-27-idempotency-keys.md) - API request deduplication
