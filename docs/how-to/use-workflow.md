# Using a Workflow via the API

Endpoint:
POST /api/workflows/{workflow_id}/start

Auth: required. User must have EXECUTOR role in the workflow’s org.

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

## Which Mode Should I Use?

- Raw body: simplest, when you control headers (backend services, curl).
- JSON envelope: when you must bundle metadata and can’t rely on custom headers.
- Multipart: large files or browser uploads.

All modes end up identical after ingestion: a Submission plus a queued ValidationRun.
