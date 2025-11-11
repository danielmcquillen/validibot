# Sending Data to the API

Workflows accept data submissions over three request modes so every type of API client can send content in a safe, predictable way. This guide explains when to use each mode and shows example requests you can adapt.

## Before You Start

The following instructions are for the user who will be calling the workflow via the API.
The user calling the API to run a workflow, whether you or someone else, must have EXECUTOR access to the workflow's organization.

Let's assume that you're the one calling the API.

- Review [Understanding Data Shapes & Types](data-shapes.md) to confirm how your source data should be represented and which MIME type fits best.
- If you don't have your API key handy, the can find it by logging into SimpleValidations and navigating to your user profile.
- Locate the URL of the workflow you want to call (visible in the 'Workflow details' portion of the workflow page).
- Confirm the MIME type of the payload you plan to send (`application/json`, `application/xml`, plain text, etc.).
- Decide whether you need to attach metadata (for example `{"source": "api"}`) alongside the content.
  The endpoint is always `POST /api/workflows/<workflow_id>/start/`.

## Mode 1 – Raw Body

Use Raw Body when your client can set HTTP headers directly and you want the lightest possible request.

- Set `Content-Type` to the payload's MIME type.
- Send the file contents as the literal request body.
- Optional headers:
  - `Content-Encoding: base64` if you base64-encode the body.
  - `X-Filename: payload.json` to control the submission name.

```bash
curl -X POST \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @payload.json \
  "https://api.simplevalidations.app/api/workflows/42/start/"
```

## Mode 2 – JSON Envelope

Choose the JSON envelope when you need to pass metadata or when your platform cannot set custom headers.

- Set `Content-Type: application/json`.
- Wrap the payload in a JSON object with explicit fields.
- The outer HTTP header stays `application/json`; use the inner `content_type`
  field to describe the data stored in `content` (for example XML or CSV).

```bash
curl -X POST \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "content": "<root><value>1</value></root>",
        "content_type": "application/xml",
        "filename": "sample.xml",
        "metadata": {"source": "supplier-api"}
      }' \
  "https://api.simplevalidations.app/api/workflows/42/start/"
```

If `content_encoding` is set to `base64`, the service will decode it before creating the submission. Arrays or objects supplied as `content` are automatically serialized to JSON strings so the stored submission always contains plain text that mirrors a real JSON file—this keeps downstream validators from having to guess at Python-native types.

## Mode 3 – Multipart Upload

Use multipart uploads for browser forms, large files, or SDKs that already support `multipart/form-data`.

- Send the file as the `file` part.
- Optional text parts: `filename`, `content_type`, `metadata` (JSON string), `content_encoding`.

```bash
curl -X POST \
  -H "Authorization: Bearer $API_TOKEN" \
  -F "file=@/path/to/building.idf" \
  -F "filename=building.idf" \
  -F "content_type=application/octet-stream" \
  -F 'metadata={"source": "browser"}' \
  "https://api.simplevalidations.app/api/workflows/42/start/"
```

Multipart requests are parsed the same way as JSON envelopes after upload completes.

## Choosing the Right Mode

| Mode             | Best For                                              | Things to Remember                                                                                         |
| ---------------- | ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Raw Body         | Server integrations, CLIs, queues                     | Set `Content-Type` accurately and include `X-Filename` when you need a friendly submission name.           |
| JSON Envelope    | Apps that need metadata or cannot send custom headers | Wrap everything in JSON and include `content_type` so the service knows how to parse the payload.          |
| Multipart Upload | Browser forms, SDKs that already support multipart    | Keep the `metadata` part as a JSON string (use single quotes around the shell argument to avoid escaping). |

## Workflow File Types

Each workflow advertises an `allowed_file_types` array (JSON, XML, TEXT, YAML, etc.) in both the in-app detail page and the `/api/workflows/` responses. Pick one of those logical types when launching from the UI—the dropdown disappears when there is only a single option—or set your HTTP `Content-Type` header accordingly when you call the API.

Validators also declare their `supported_file_types`, so a workflow that allows JSON *and* XML might still block an XML run if one of its steps only speaks JSON. In that situation (or when you send a format the workflow never accepted) the API returns `FILE_TYPE_UNSUPPORTED` with a detail that names the blocking step. The UI shows the same text at the top of the launch form.

The service re-sniffs inline content after ingesting it. If you POST `text/plain` but the payload is obviously JSON, the stored Submission is marked as JSON so downstream automation has the right classification.

## Responses and Errors

Successful requests return `201 Created` with a completed run when validation finishes immediately, or `202 Accepted` with a `Location` header you can poll while the run executes. Error responses follow a consistent structure:

```json
{
  "detail": "This workflow is not active and cannot accept new runs.",
  "code": "workflow_inactive",
  "status": 409,
  "errors": []
}
```

Log both `detail` and `code` so client applications can react appropriately. Validation errors (bad payloads, missing metadata) populate the `errors` array with field-level messages.

## Troubleshooting Tips

- **409 workflow_inactive**: Re-enable the workflow from the UI before retrying.
- **400 no_workflow_steps**: Add at least one active step to the workflow.
- **400 file_type_unsupported**: Check the workflow's `allowed_file_types` (UI or API) and make sure every validator in the workflow supports the format you are sending.
- **415 unsupported_media_type**: Double-check the `content_type` you sent; it must match one of the supported formats listed in the workflow launch form.

If a request looks correct but still fails, capture the response headers and body, then share them with support along with the workflow ID and timestamp.

## Related Guides

- [API Overview](../api-overview.md)
- [Authentication](authentication.md)
- [Webhooks & Notifications](webhooks.md)
