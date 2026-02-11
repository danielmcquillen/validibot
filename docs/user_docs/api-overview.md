# API Overview

Validibot provides a REST API for programmatic access to validation workflows. This guide introduces the API structure and concepts. For implementation details, see the linked guides.

## Base URL

The API base URL depends on your deployment:

- **Your deployment**: `https://your-domain.com/api/v1/`
- **Example**: `https://validibot.example.com/api/v1/`

All API endpoints are versioned under `/api/v1/`. The current version is v1.

## Authentication

All API requests require authentication using a Bearer token:

```bash
curl -H "Authorization: Bearer YOUR_API_TOKEN" \
  "https://your-validibot.com/api/v1/orgs/my-org/workflows/"
```

To get an API token:

1. Log into Validibot
2. Go to your profile settings
3. Navigate to API Tokens
4. Create a new token and copy it immediately (you won't see it again)

For detailed authentication instructions, see [Authentication](api/authentication.md).

## URL Structure

Validibot's API is organized around organizations. Most endpoints follow this pattern:

```
/api/v1/orgs/{org_slug}/resources/
```

For example:

- List workflows: `GET /api/v1/orgs/my-org/workflows/`
- Get a specific workflow: `GET /api/v1/orgs/my-org/workflows/my-workflow/`
- Start a validation: `POST /api/v1/orgs/my-org/workflows/my-workflow/runs/`
- Get run details: `GET /api/v1/orgs/my-org/runs/{run_id}/`

The `{org_slug}` is your organization's URL-friendly identifier. The `{workflow_identifier}` can be either the workflow's slug (recommended) or its numeric ID.

## Common Request Patterns

### Content Types

Most endpoints expect and return JSON:

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key": "value"}' \
  "https://your-validibot.com/api/v1/..."
```

When submitting data for validation, you can also use:

- Raw body with appropriate Content-Type header
- Multipart form data for file uploads

See [Sending Data to the API](api/sending-data.md) for all submission modes.

### HTTP Methods

| Method | Purpose |
|--------|---------|
| `GET` | Retrieve resources |
| `POST` | Create resources, start validations |
| `PUT` / `PATCH` | Update resources |
| `DELETE` | Remove resources |

### Pagination

List endpoints return paginated results:

```json
{
  "count": 42,
  "next": "https://your-validibot.com/api/v1/.../workflows/?page=2",
  "previous": null,
  "results": [...]
}
```

Use the `next` URL to fetch additional pages.

## Starting a Validation

The most common API operation is submitting data for validation:

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @payload.json \
  "https://your-validibot.com/api/v1/orgs/my-org/workflows/my-workflow/runs/"
```

This requires **Executor** role or higher in the organization.

**Response codes**:

- `201 Created`: Validation completed; response includes full results
- `202 Accepted`: Validation started; poll the provided URL for status
- `4xx`: Client error (see error format below)
- `5xx`: Server error

For complete details on submitting data, see [Sending Data to the API](api/sending-data.md).

## Error Format

Errors follow a consistent structure:

```json
{
  "detail": "This workflow is not active and cannot accept new runs.",
  "code": "workflow_inactive",
  "status": 409,
  "errors": []
}
```

| Field | Description |
|-------|-------------|
| `detail` | Human-readable error message |
| `code` | Machine-readable error code (stable, use for programmatic handling) |
| `status` | HTTP status code (also in response headers) |
| `errors` | Array of field-specific errors for validation failures |

### Common Error Codes

| Code | Status | Meaning |
|------|--------|---------|
| `authentication_required` | 401 | Missing or invalid token |
| `permission_denied` | 403 | Token lacks required permissions |
| `not_found` | 404 | Resource doesn't exist |
| `workflow_inactive` | 409 | Workflow is not active |
| `no_workflow_steps` | 400 | Workflow has no validation steps |
| `file_type_unsupported` | 400 | Submitted file type not accepted |
| `rate_limited` | 429 | Too many requests |

## Permission Requirements

API operations require specific roles:

| Operation | Minimum Role |
|-----------|--------------|
| List workflows | Viewer |
| View workflow details | Viewer |
| Start validation run | Executor |
| View run results | Viewer |
| Create/edit workflows | Author |
| Manage organization | Admin |

## Rate Limiting

The API enforces rate limits to ensure fair usage. If you exceed limits, you'll receive a `429 Too Many Requests` response with a `Retry-After` header indicating when to retry.

Best practices:

- Implement exponential backoff for retries
- Cache responses when appropriate
- Batch operations when possible

## Next Steps

- [Authentication](api/authentication.md) — Token management and security
- [Sending Data to the API](api/sending-data.md) — Submission modes and examples
- [Data Shapes](api/data-shapes.md) — Understanding content types and MIME types
- [Webhooks](api/webhooks.md) — Receive notifications when validations complete
