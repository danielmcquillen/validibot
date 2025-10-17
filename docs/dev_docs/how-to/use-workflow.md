# Using a Workflow via the API

Endpoint:
POST /api/workflows/{workflow_id}/start

Auth: required. User must have EXECUTOR role in the workflow’s org.
Workflow status: the workflow must be **active**. Disabled workflows return HTTP 403 with `{"detail": "This workflow is inactive..."}` and no run is created.

Feature flag: set `ENABLE_API=True` (default) to expose these endpoints. When the flag is `False`, all `/api/v1/` routes return 404.

You can submit content in three ways:

## Mode 1: Raw Body (Header-Driven)

Body: raw bytes of the document.

Headers:
Content-Type: application/json | application/xml | text/plain | text/x-idf
(optional) Content-Encoding: base64
(optional) X-Filename: model.idf

Example (raw JSON):
curl -X POST https://api.example.com/api/workflows/42/start \
 -H "Authorization: Bearer <token>" \
 -H "Content-Type: application/json" \
 --data-binary @building.json

Example (raw XML):
curl -X POST https://api.example.com/api/workflows/42/start \
 -H "Authorization: Bearer <token>" \
 -H "Content-Type: application/xml" \
 --data-binary '<root><item>1</item></root>'

Base64 (when necessary):
curl -X POST https://api.example.com/api/workflows/42/start \
 -H "Authorization: Bearer <token>" \
 -H "Content-Type: text/x-idf" \
 -H "Content-Encoding: base64" \
 --data "$(base64 building.idf)"

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
curl -X POST https://api.example.com/api/workflows/42/start \
 -H "Authorization: Bearer <token>" \
 -H "Content-Type: application/json" \
 -d '{
"content": "{\"building\":\"A\"}",
"content_type": "application/json",
"filename": "bldg.json",
"metadata": {"ticket":"ABC-1"}
}'

## Mode 3: Multipart Upload

Content-Type: multipart/form-data

Parts:
file: (binary file)
metadata: JSON string (optional)
filename: (optional)
content_type: (optional override)

Example:
curl -X POST https://api.example.com/api/workflows/42/start \
 -H "Authorization: Bearer <token>" \
 -F "file=@building.idf" \
 -F 'metadata={\"source\":\"browser\"}' \
 -F "filename=building.idf" \
 -F "content_type=text/x-idf"

## Responses

If workflow finishes quickly (optimistic window):
201 Created
Location: /api/validation-runs/<id>/

Else (still processing):
202 Accepted
Location: /api/validation-runs/<id>/
Retry-After: 5

Poll the Location until status is SUCCEEDED or FAILED.

### Error responses

- 409 Conflict with `code: "WORKFLOW_INACTIVE"` (empty detail) when the target workflow is inactive.
- 400 Bad Request with `code: "NO_WORKFLOW_STEPS"` when no validation steps are configured for the workflow.
- Other validation errors reuse HTTP status codes (400/413) without custom `code` values; rely on the standard `detail` message.

## Which Mode Should I Use?

- Raw body: simplest, when you control headers (backend services, curl).
- JSON envelope: when you must bundle metadata and can’t rely on custom headers.
- Multipart: large files or browser uploads.

## Run from the App UI

When teammates need to test a workflow without writing code, send them to
`/app/workflows/<workflow id>/launch/`. The page shows two tabs:

- **Info** – read-only metadata about the workflow, steps, and recent runs.
- **Run** – an inline form that accepts either pasted content or an uploaded
  file. As soon as they submit, the page streams run status updates until the
  run succeeds or fails.

Users still need the EXECUTOR role in the workflow’s organization to access the
Run tab. If you enable the *Make info public* flag, the Info tab is also
available without authentication at `/workflows/<workflow uuid>/info`.

All modes end up identical after ingestion: a Submission plus a queued ValidationRun.
