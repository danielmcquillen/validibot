# Using a Workflow via the API

Endpoint:
```
POST /api/v1/orgs/{org_slug}/workflows/{workflow_identifier}/runs/
```

The `workflow_identifier` can be either the workflow's slug (preferred) or its numeric database ID.

Auth: required. User must be a member of the org and have EXECUTOR role.
Workflow status: the workflow must be **active**. Disabled workflows return HTTP 403 with `{"detail": "This workflow is inactive..."}` and no run is created.

Feature flag: set `ENABLE_API=True` (default) to expose these endpoints. When the flag is `False`, all `/api/v1/` routes return 404.

See [ADR-2026-01-06](../adr/2026-01-06-org-scoped-routing-and-versioned-workflow-identifiers.md) for details on org-scoped routing.

You can submit content in three ways:

## Idempotency keys: safer retries

Add an `Idempotency-Key` header (a UUIDv4 is perfect) to every state-changing call so you can retry safely if the network drops:

```
-H "Idempotency-Key: 8f14e45f-ceea-467f-a8ad-0e7e3a1a8b9c"
```

What you get:
- If the first request finishes, we cache its final response for 24 hours and return that same response on any repeat with the same key. You avoid duplicate runs, double billing, and noisy findings.
- If a duplicate arrives while the first is still running, we return `409 Conflict` to say “still working on it; try again later” instead of starting another run.
- When a cached response is replayed you’ll see headers: `Idempotent-Replayed: true` and `Original-Request-Id: <uuid>`.
- If you skip the header, we still process the request, but retries can create duplicate runs because there is nothing to correlate them. Use a fresh UUID per request.

Developer policy: every mutating API endpoint should be wrapped with the idempotency decorator; keep the header optional for now but always include it in examples so clients adopt it.

## Mode 1: Raw Body (Header-Driven)

Body: raw bytes of the document.

Headers:
Content-Type: application/json | application/xml | text/plain | text/x-idf
(optional) Content-Encoding: base64
(optional) X-Filename: model.idf

Example (raw JSON):
```bash
curl -X POST https://api.example.com/api/v1/orgs/my-org/workflows/my-workflow/runs/ \
 -H "Authorization: Bearer <token>" \
 -H "Idempotency-Key: 8f14e45f-ceea-467f-a8ad-0e7e3a1a8b9c" \
 -H "Content-Type: application/json" \
 --data-binary @building.json
```

Example (raw XML):
```bash
curl -X POST https://api.example.com/api/v1/orgs/my-org/workflows/my-workflow/runs/ \
 -H "Authorization: Bearer <token>" \
 -H "Idempotency-Key: 8f14e45f-ceea-467f-a8ad-0e7e3a1a8b9c" \
 -H "Content-Type: application/xml" \
 --data-binary '<root><item>1</item></root>'
```

Base64 (when necessary):
```bash
curl -X POST https://api.example.com/api/v1/orgs/my-org/workflows/my-workflow/runs/ \
 -H "Authorization: Bearer <token>" \
 -H "Idempotency-Key: 8f14e45f-ceea-467f-a8ad-0e7e3a1a8b9c" \
 -H "Content-Type: text/x-idf" \
 -H "Content-Encoding: base64" \
 --data "$(base64 building.idf)"
```

## Mode 2: JSON Envelope

Content-Type: application/json
Body JSON:
{
"content": "<string or base64 if content_encoding=base64>",
"content_type": "application/xml",
"content_encoding": "base64",
"filename": "building.idf",
"metadata": { "source": "ui" }
}

Example:
```bash
curl -X POST https://api.example.com/api/v1/orgs/my-org/workflows/my-workflow/runs/ \
 -H "Authorization: Bearer <token>" \
 -H "Idempotency-Key: 8f14e45f-ceea-467f-a8ad-0e7e3a1a8b9c" \
 -H "Content-Type: application/json" \
 -d '{
"content": "{\"building\":\"A\"}",
"content_type": "application/json",
"filename": "bldg.json",
"metadata": {"ticket":"ABC-1"}
}'
```

## Mode 3: Multipart Upload

Content-Type: multipart/form-data

Parts:
file: (binary file)
metadata: JSON string (optional)
filename: (optional)
content_type: (optional override)

Example:
```bash
curl -X POST https://api.example.com/api/v1/orgs/my-org/workflows/my-workflow/runs/ \
 -H "Authorization: Bearer <token>" \
 -H "Idempotency-Key: 8f14e45f-ceea-467f-a8ad-0e7e3a1a8b9c" \
 -F "file=@building.idf" \
 -F 'metadata={\"source\":\"browser\"}' \
 -F "filename=building.idf" \
 -F "content_type=text/x-idf"
```

## Responses

If workflow finishes quickly (optimistic window):
```
201 Created
Location: /api/v1/orgs/{org_slug}/runs/{run_id}/
```

Else (still processing):
```
202 Accepted
Location: /api/v1/orgs/{org_slug}/runs/{run_id}/
Retry-After: 5
```

Poll the Location until status is SUCCEEDED or FAILED.

### Error responses

- 409 Conflict with `code: "WORKFLOW_INACTIVE"` (empty detail) when the target workflow is inactive.
- 400 Bad Request with `code: "NO_WORKFLOW_STEPS"` when no validation steps are configured for the workflow.
- 400 Bad Request with `code: "FILE_TYPE_UNSUPPORTED"` when the submission's logical file type is not accepted by the workflow or by at least one step.
- Other validation errors reuse HTTP status codes (400/413) without custom `code` values; rely on the standard `detail` message.

## Which Mode Should I Use?

- Raw body: simplest, when you control headers (backend services, curl).
- JSON envelope: when you must bundle metadata and can’t rely on custom headers.
- Multipart: large files or browser uploads.

## Run from the App UI

When teammates need to test a workflow without writing code, send them to
`/app/workflows/<workflow id>/launch/`. The page shows two tabs:

- **Info** – read-only metadata about the workflow, steps, and recent runs.
- **Run** – a submission form that accepts either pasted content or an uploaded
  file. Successful submissions redirect to `/app/workflows/<workflow id>/launch/run/<run id>/`,
  where the run status panel polls via HTMX until the run succeeds, fails, or is cancelled.

Users still need the EXECUTOR role in the workflow’s organization to access the
Run tab. If you enable the *Make info public* flag, the Info tab is also
available without authentication at `/workflows/<workflow uuid>/info`.

All modes end up identical after ingestion: a Submission plus a queued ValidationRun.

## File type expectations

- Every workflow stores an `allowed_file_types` array (JSON, XML, TEXT, YAML, etc.). Authors select these options on the workflow form; the launch UI now renders a dropdown only when there is more than one choice.
- Every validator has a `supported_file_types` list. System validators get sane defaults, and custom validators must be explicit. The step builder only offers validators that overlap with the workflow's current allow list.
- During launch we re-inspect the payload. If we can prove the content is JSON even though it was submitted as `text/plain`, we store the Submission as JSON so downstream tooling has the right classification.
- If a client sends a format the workflow doesn't allow—or one of the validators can't consume—the API returns `FILE_TYPE_UNSUPPORTED` with a detail that names the blocking step. The UI sticks the same message at the top of the launch form.
- Use the `allowed_file_types` field exposed by `GET /api/v1/orgs/{org_slug}/workflows/` (and in the in-app workflow detail) to inform your integrations about the acceptable formats.
