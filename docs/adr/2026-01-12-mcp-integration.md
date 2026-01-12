# ADR: Model Context Protocol (MCP) Integration

**Date:** 2026-01-12
**Status:** Proposed
**Context:** Enabling AI assistants to interact with Validibot programmatically via MCP

## Summary

This ADR documents the design decisions for implementing a Model Context Protocol (MCP) server that allows AI assistants (Claude, Cursor, etc.) to submit files for validation, check run status, and retrieve results. The goal is to enable AI-assisted workflows while reusing the existing REST API infrastructure.

## Background

The Model Context Protocol (MCP) is an open standard (released by Anthropic in late 2024, with OAuth 2.1 added in March 2025) that enables LLM applications to interact with external data sources and tools. Major AI platforms including Claude, OpenAI, and Cursor now support MCP, making it a valuable integration point for SaaS applications.

### Use Cases

1. **AI-Assisted Validation**: Users ask Claude Code to "validate this IDF file against my energy code workflow"
2. **Automated Remediation**: AI reviews validation findings and suggests fixes
3. **CI/CD Integration**: AI agents include validation in automated pipelines
4. **Interactive Debugging**: AI helps users understand validation results and iterate

### Research Summary

Based on research into current best practices:

- [MCP Best Practices for Scalable AI Integrations](https://www.marktechpost.com/2025/07/23/7-mcp-server-best-practices-for-scalable-ai-integrations-in-2025/) recommends focused toolsets (not mapping every API endpoint)
- [Agent Interviews' MCP-Django implementation](https://docs.agentinterviews.com/blog/mcp-server-django-implementation/) found running MCP as a separate container wrapping existing API worked best
- [django-mcp-server](https://github.com/omarbenhamid/django-mcp-server) provides patterns for Django integration
- [MCP OAuth 2.1 specification](https://modelcontextprotocol.io/docs/tutorials/security/authorization) recommends OAuth for production but notes API keys are acceptable for simpler deployments
- [Prefect](https://docs.prefect.io/v3/api-ref/rest-api) and [Dagster](https://dagster.io/blog/when-sync-isnt-enough) use polling patterns for async job status, similar to our CLI

## Design Decisions

### 1. Architecture: Separate MCP Server Package

**Decision:** Create a standalone MCP server package (`validibot-mcp`) that wraps the existing REST API.

**Alternatives Considered:**

| Option | Pros | Cons |
|--------|------|------|
| **A. Separate package (chosen)** | Isolation, independent deployment, no Django conflicts | Extra package to maintain |
| B. Embed in Django via django-mcp-server | Single deployment | Dependency conflicts, couples concerns |
| C. Add MCP endpoints to existing API | Reuses infrastructure | Mixes HTTP semantics with MCP protocol |

**Rationale:**

- The [Agent Interviews team](https://docs.agentinterviews.com/blog/mcp-server-django-implementation/) found that wrapping existing API endpoints as MCP tools worked better than embedding
- Avoids dependency conflicts between MCP SDK and Django packages
- Allows independent iteration and deployment
- Follows the pattern of our existing `validibot-cli` package

**Location:** `../validibot-mcp` (sibling to `validibot-cli`)

### 2. Authentication: Bearer Token API Keys (Phase 1)

**Decision:** Use existing API keys with Bearer token authentication, extracted from MCP context headers.

**Alternatives Considered:**

| Option | Pros | Cons |
|--------|------|------|
| **A. API keys via context headers (chosen)** | Works today, no backend changes, users already have keys | Not OAuth 2.1 compliant |
| B. Full OAuth 2.1 flow | Spec compliant, token refresh, scoped permissions | Complex, requires django-oauth-toolkit setup |
| C. API keys as tool parameters | Simple | Exposes keys in tool call logs |

**Rationale:**

- Users already have API keys from `https://validibot.com/app/users/api-key/`
- The CLI already uses Bearer token auth successfully
- [Scalekit's guidance](https://www.scalekit.com/blog/migrating-from-api-keys-to-oauth-mcp-servers) notes API keys are acceptable for early-stage integrations
- OAuth 2.1 can be added later if enterprise customers require it

**Implementation:**

```python
def get_api_key_from_context() -> str:
    """Extract Bearer token from MCP request context headers."""
    # MCP clients pass headers in the request context
    # Users configure: VALIDIBOT_TOKEN=<api-key> in their MCP client
    auth_header = mcp_context.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    raise AuthenticationError("Missing or invalid Authorization header")
```

**Phase 2 (Future):** Add OAuth 2.1 support via django-oauth-toolkit if needed:

- Implement `/.well-known/oauth-protected-resource` metadata endpoint
- Add `/oauth/authorize` and `/oauth/token` endpoints
- Support PKCE and dynamic client registration per MCP spec

### 3. Transport Protocol: Streamable HTTP

**Decision:** Use HTTP/SSE transport for the remote MCP server.

**Rationale:**

- Claude AI supports streamable HTTP as of June 2025
- STDIO is only for local development tools, not SaaS integration
- HTTP transport enables deployment on Cloud Run

**Endpoint:** `https://validibot.com/mcp/` or `https://mcp.validibot.com/`

### 4. Tool Design: Focused Toolset

**Decision:** Expose 4-5 high-level tools rather than mapping every API endpoint.

**Rationale:**

- [MCP best practices](https://www.marktechpost.com/2025/07/23/7-mcp-server-best-practices-for-scalable-ai-integrations-in-2025/) recommend focused toolsets to reduce complexity
- Users want to "validate a file" not "create a submission, then create a run, then poll for status"
- Higher-level tools reduce the chance of AI making mistakes

**Proposed Tools:**

| Tool | Description | Parameters | Returns |
|------|-------------|------------|---------|
| `list_workflows` | List available workflows with descriptions | `org` (optional) | Array of workflow summaries |
| `get_workflow_details` | Get workflow info including expected file types | `workflow_id`, `org` (optional) | Workflow details |
| `validate_file` | Submit a file for validation | `workflow_id`, `file_content` (base64), `file_name`, `org` (optional) | `run_id`, initial status |
| `get_run_status` | Check run status and get findings | `run_id`, `org` (optional) | Status, result, findings, output signals |
| `wait_for_run` | Poll until run completes (with timeout) | `run_id`, `timeout_seconds`, `org` (optional) | Final status, result, findings |

**Tool Schemas (JSON Schema format per MCP spec):**

```python
@mcp.tool()
async def validate_file(
    workflow_id: str,
    file_content: str,  # base64 encoded
    file_name: str,
    org: str | None = None,
) -> dict:
    """
    Submit a file for validation against a workflow.

    Args:
        workflow_id: Workflow ID or slug to validate against
        file_content: Base64-encoded file content
        file_name: Original filename (used for file type detection)
        org: Organization slug (required if workflow slug is ambiguous)

    Returns:
        run_id: The validation run ID for checking status
        status: Initial run status (usually "PENDING" or "RUNNING")
        message: Human-readable status message
    """
```

### 5. Async Run Handling: Polling with Timeout

**Decision:** Provide both immediate return (`get_run_status`) and blocking (`wait_for_run`) patterns.

**Rationale:**

- EnergyPlus validations can take minutes
- Matches patterns used by [Dagster](https://dagster.io/blog/when-sync-isnt-enough) and our CLI
- AI can choose: quick check or wait for completion

**Implementation:**

```python
@mcp.tool()
async def wait_for_run(
    run_id: str,
    timeout_seconds: int = 300,
    org: str | None = None,
) -> dict:
    """
    Wait for a validation run to complete, polling periodically.

    Args:
        run_id: The validation run ID
        timeout_seconds: Maximum time to wait (default: 5 minutes)
        org: Organization slug

    Returns:
        status: Final run status
        result: PASS, FAIL, ERROR, or TIMED_OUT
        is_complete: Whether the run finished
        findings: List of validation findings (if complete)
        output_signals: Dict of output values from async validators
    """
    poll_interval = 5  # seconds
    start = time.time()

    while True:
        run = await client.get_validation_run(run_id, org)

        if run.is_complete:
            return format_run_result(run)

        if time.time() - start > timeout_seconds:
            return {
                "status": run.status,
                "result": "TIMED_OUT",
                "is_complete": False,
                "message": f"Run still in progress after {timeout_seconds}s",
            }

        await asyncio.sleep(poll_interval)
```

### 6. File Handling: Base64 Content

**Decision:** Accept file content as base64-encoded strings.

**Alternatives Considered:**

| Option | Pros | Cons |
|--------|------|------|
| **A. Base64 in tool parameters (chosen)** | Works with all MCP clients, no file system access needed | Size limits, encoding overhead |
| B. File paths | Direct access | Requires local file system, security concerns |
| C. Pre-signed upload URLs | Handles large files | Two-step process, complexity |

**Rationale:**

- MCP clients typically read files and pass content
- Most validation files (IDF, JSON, XML) are text-based and reasonably sized
- Matches how our existing API handles multipart uploads

**Size Limit:** 10MB encoded (aligned with existing API limits)

### 7. Deployment: Cloud Run Container

**Decision:** Deploy as a separate Cloud Run service.

**Configuration:**

```yaml
# Dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install -e .
CMD ["python", "-m", "validibot_mcp.server"]

# Cloud Run configuration
service: validibot-mcp
region: us-central1
cpu: 1
memory: 512Mi
min-instances: 0
max-instances: 10
```

**Rationale:**

- Matches existing Cloud Run deployment pattern
- Scales independently from main Django app
- Zero cold-start cost with min-instances: 0

### 8. Error Handling

**MCP Error Responses:**

| HTTP Status | MCP Error | When |
|-------------|-----------|------|
| 401 | `UNAUTHORIZED` | Missing or invalid API key |
| 403 | `FORBIDDEN` | User lacks access to workflow/run |
| 404 | `NOT_FOUND` | Workflow or run doesn't exist |
| 400 | `INVALID_PARAMS` | Bad workflow slug, missing org for ambiguous lookup |
| 500 | `INTERNAL_ERROR` | Server errors |

**Error Response Format:**

```json
{
  "error": {
    "code": "INVALID_PARAMS",
    "message": "Multiple workflows match 'energy-model'. Specify org parameter.",
    "data": {
      "matches": [
        {"org": "acme-corp", "version": 1},
        {"org": "demo-org", "version": 2}
      ]
    }
  }
}
```

## Implementation Plan

### Phase 1: Minimal Viable MCP Server (~1 week)

1. **Package setup** (1 day)
   - Create `validibot-mcp` package with pyproject.toml
   - Add MCP SDK dependency (`mcp>=1.25.0`)
   - Set up basic server structure with FastMCP

2. **Core tools** (2-3 days)
   - Implement `list_workflows` and `get_workflow_details`
   - Implement `validate_file` with base64 handling
   - Implement `get_run_status` and `wait_for_run`
   - Add API key extraction from context

3. **Deployment** (1 day)
   - Create Dockerfile
   - Configure Cloud Run
   - Set up routing (`/mcp/` or subdomain)

4. **Documentation** (1 day)
   - User guide for connecting Claude Desktop/Cursor
   - API key setup instructions
   - Example prompts and workflows

### Phase 2: Enhancements (Future)

- OAuth 2.1 support for enterprise customers
- Webhook notifications instead of polling
- File upload via pre-signed URLs for large files
- MCP Resources for browsing past runs

## Package Structure

```
validibot-mcp/
├── pyproject.toml
├── README.md
├── Dockerfile
├── src/
│   └── validibot_mcp/
│       ├── __init__.py
│       ├── server.py          # FastMCP server setup
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── workflows.py   # list_workflows, get_workflow_details
│       │   ├── validate.py    # validate_file
│       │   └── runs.py        # get_run_status, wait_for_run
│       ├── client.py          # API client (shared with CLI or separate)
│       ├── auth.py            # API key extraction
│       └── config.py          # Settings
└── tests/
    └── ...
```

## Security Considerations

1. **API Key Handling**
   - Keys extracted from headers, never logged or stored
   - Keys scoped to user's organization access
   - Existing rate limiting applies (60 req/min for validation)

2. **Input Validation**
   - File content validated against allowed types
   - Workflow/org slugs sanitized
   - Size limits enforced

3. **Audit Trail**
   - All validation runs logged with source="mcp"
   - Existing ValidationRun audit trail preserved

4. **TLS**
   - HTTPS required for production
   - Cloud Run handles TLS termination

## Consequences

**Positive:**

- Users can interact with Validibot through AI assistants
- Reuses existing API infrastructure and authentication
- Minimal implementation effort (~1 week)
- Independent deployment and scaling

**Negative:**

- Additional package to maintain
- API key auth is not OAuth 2.1 compliant (acceptable for Phase 1)
- Polling for async runs is not ideal (could add webhooks later)

**Risks:**

- MCP protocol may evolve (mitigated by using official SDK)
- AI hallucinations could cause unexpected tool calls (mitigated by focused toolset)

## Related

- [ADR-2025-12-22: CLI and API Support](2025-12-22-cli-api-support.md) - Existing API authentication
- [ADR-2026-01-06: Org-Scoped Web URLs](2026-01-06-org-scoped-web-urls.md) - API routing patterns
- [Model Context Protocol Specification](https://modelcontextprotocol.io/specification/2025-11-25)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [django-mcp-server](https://github.com/omarbenhamid/django-mcp-server)

## References

- [MCP Best Practices for Scalable AI Integrations (2025)](https://www.marktechpost.com/2025/07/23/7-mcp-server-best-practices-for-scalable-ai-integrations-in-2025/)
- [Agent Interviews: MCP-Django Implementation](https://docs.agentinterviews.com/blog/mcp-server-django-implementation/)
- [MCP OAuth 2.1 Authorization](https://modelcontextprotocol.io/docs/tutorials/security/authorization)
- [Migrating from API Keys to OAuth for MCP Servers](https://www.scalekit.com/blog/migrating-from-api-keys-to-oauth-mcp-servers)
- [Prefect REST API Documentation](https://docs.prefect.io/v3/api-ref/rest-api)
- [Dagster Async Execution Patterns](https://dagster.io/blog/when-sync-isnt-enough)
