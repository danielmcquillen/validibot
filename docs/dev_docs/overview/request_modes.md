# Workflow Submission Modes

Every workflow run begins with an HTTP request to `POST /api/v1/orgs/{org_slug}/workflows/{workflow_identifier}/runs/`. Callers can shape that request three different ways, and the view layer auto-detects the correct parsing route.

See [ADR-2026-01-06](../adr/2026-01-06-org-scoped-routing-and-versioned-workflow-identifiers.md) for details on org-scoped API routing.

This page documents each "mode", the detection rules in `validibot/workflows/request_utils.py`, and the expectations downstream services rely on.

## Quick Reference Matrix

| Mode                                    | Trigger                                                                                  | Required `Content-Type`                                                      | Transport shape                                                     | Typical clients                                               |
| --------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------- |
| **1&nbsp;·&nbsp;Raw&nbsp;Body**         | Raw body (non-multipart) request whose `Content-Type` matches `SUPPORTED_CONTENT_TYPES`. | Any value from `SUPPORTED_CONTENT_TYPES` (JSON, XML, plain text, IDF, etc.). | Literal body bytes; optional headers for filename/encoding.         | CLI tools, backend jobs, queue workers.                       |
| **2&nbsp;·&nbsp;JSON&nbsp;Envelope**    | `Content-Type: application/json` and body parses to an object with a `content` key.      | `application/json`                                                           | `{ "content": "...", "content_type": "...", ... }`                  | Integrations that need metadata or cannot set custom headers. |
| **3&nbsp;·&nbsp;Multipart&nbsp;Upload** | `Content-Type: multipart/form-data`.                                                     | `multipart/form-data`                                                        | File part plus optional text fields (`metadata`, `filename`, etc.). | Browser uploads, SDKs with multipart helpers.                 |

All three shapes eventually become the same pair of records: a `Submission` row that holds the submitted data plus a queued `ValidationRun` that
maintains the state of the run.

## Mode 1 – Raw Body

### Trigger & Requirements

- Request is **not** `multipart/*`.
- `Content-Type` header matches one of the entries in `SUPPORTED_CONTENT_TYPES` (`validibot/workflows/constants.py`). If the header is missing or unknown, the system refuses to treat it as Mode 1.
- When `Content-Type` is `application/json`, we inspect the first byte and (cheaply) parse JSON to ensure the payload is not actually a Mode-2 envelope.

### Optional Headers

- `Content-Encoding: base64` — server decodes before creating the submission.
- `X-Filename: name.ext` — used for UI reporting; otherwise the workflow slug plus timestamp is used.

### Implementation Notes

- `extract_request_basics` normalizes the header by stripping parameters and lowercasing it.
- `detect_mode` enforces the rules above and returns a structured result (`SubmissionRequestMode`, parsed JSON, and any detection errors).
- When a request passes the check, `WorkflowViewSet._handle_raw_body_mode` stores the bytes directly on the `Submission` without touching serializers.

### Failure Modes

- Missing/unsupported `Content-Type` → falls through to serializer path, which will raise `415`/`400` as appropriate.
- Base64 flag present but payload is not base64 → `binascii.Error` surfaces as a `400` with `content_encoding` errors.

### TODOs & Questions

- **Streaming**: we currently read the entire body into memory. Should we add chunked streaming for >50 MB payloads?
- **Checksum headers**: consider requiring `Content-MD5` for large binaries.

## Mode 2 – JSON Envelope

### Trigger & Shape

- `Content-Type` **must** be `application/json`.
- Body is a JSON object containing at least a `content` field. Additional fields supported today: `content_type`, `filename`, `metadata`, `content_encoding`.
- The outer HTTP header **always** stays `application/json`; the inner
  `content_type` field describes the payload stored inside `content` (for
  example XML, CSV, or IDF). We validate that value against
  `SUPPORTED_CONTENT_TYPES`.

```json
{
  "content": "<root><value>1</value></root>",
  "content_type": "application/xml",
  "filename": "sample.xml",
  "metadata": { "source": "api" }
}
```

### Serializer Behaviour

- Parsed by `ValidationRunStartSerializer`.
- If `content` is already a list/dict (e.g., `{ "status": true }`), we JSON-dump it before storing so downstream validators always see text.
- That JSON-dump step ensures every submission stores the same plain-text representation, regardless of whether the caller sent raw JSON or DRF handed us a Python object. Validators can safely treat submissions as `str`/bytes without inspecting Python-native types.
- Optional `content_encoding: "base64"` is decoded server-side.

### Notes & TODOs

- **Field validation** lives in the serializer; add new optional fields there first so documentation stays accurate.
- **Future consideration**: do we need a separate field for attachments (e.g., manifest + multiple files)? Add a placeholder section when we design it.

## Mode 3 – Multipart Upload

### Trigger & Shape

- `Content-Type: multipart/form-data`.
- Primary file part named `file` (handled by DRF parser).
- Optional text parts: `filename`, `content_type`, `metadata` (JSON string), `content_encoding`.

### Behaviour

- Also parsed by `ValidationRunStartSerializer`; the serializer ensures exactly one of `file` or `normalized_content` is present.
- Multipart is the best fit for browser uploads, SDKs, or any situation where you want the server to handle temporary file storage automatically.

### TODOs

- Document whether we want to accept multiple files in the future. If so, we will either archive them server-side or require callers to upload a ZIP via Mode 1/3.

## Detection Pipeline (Server-Side)

`WorkflowViewSet.start_validation` orchestrates the flow:

1. Call `extract_request_basics(request)` to capture the normalized `Content-Type` and raw body bytes.
2. Pass those values to `detect_mode(...)`.
   - `RAW_BODY` → invoke `_handle_raw_body_mode`, which builds the `Submission` immediately.
   - `JSON_ENVELOPE` → `_handle_json_envelope` uses the parsed JSON from `detect_mode` and avoids reparsing.
   - `MULTIPART` → `_handle_multipart_mode` delegates to DRF's uploaded-file handling.
   - `UNKNOWN` with an error → return `400 INVALID_PAYLOAD` with the logged reason.
3. After validation, `_process_structured_payload` produces a `Submission`, attaches metadata, and queues the `ValidationRun`.

Pseudo flow:

```python
content_type, body = extract_request_basics(request)
result = detect_mode(request, content_type, body)
if result.mode is SubmissionRequestMode.RAW_BODY:
    return self._handle_raw_body_mode(...)
if result.mode is SubmissionRequestMode.JSON_ENVELOPE:
    return self._handle_json_envelope(..., envelope=result.parsed_envelope)
if result.mode is SubmissionRequestMode.MULTIPART:
    return self._handle_multipart_mode(...)
return Response(... INVALID_PAYLOAD ...)
```

## Shared Serializer Responsibilities (Modes 2 & 3)

Once a request flows into `_process_structured_payload` (either from the JSON envelope or multipart handler) the serializer enforces a few guarantees:

- **Single content source:** Exactly one of `file` or `normalized_content` may be populated. This prevents callers from sneaking both an inline string and an uploaded file into the same run.
- **Metadata normalization:** The serializer loads any `metadata` JSON into a Python dict so downstream services always receive a consistent structure. When omitted we store an empty dict. System administrators can require flat key/value pairs or enforce a byte limit via the [Site Settings](system_admin.md) page.
- **Project context:** We attach the project resolved earlier in the view so the resulting `Submission` and `ValidationRun` inherit either the workflow’s default project or an explicit override passed via query string.
- **Consistent API errors:** Business-rule failures (inactive workflow, no steps, invalid payload) emit `WorkflowStartErrorCode` values so clients can react programmatically instead of scraping human-readable messages.

## Testing & Tooling

- End-to-end coverage lives in `validibot/workflows/tests/test_workflow_start_api.py`.
- Serializer unit tests: `validibot/validations/tests/test_serializers.py`.
- Mode detection helper tests: add to `validibot/workflows/tests/test_request_utils.py` (TODO: create if we add more branching logic).

## Open Questions & Future Considerations

1. **Large Payload Strategy** — Do we enforce explicit size caps per mode, or delegate throttling to Django/NGINX? Add decision notes here when settled.
2. **Async callbacks** — Should Mode 1 have a streaming variant for real-time checks? Currently every mode waits for the full upload.
3. **Enhanced metadata** — Do we want to reserve keys (e.g., `source_system`, `batch_id`) for downstream analytics? Until we decide, metadata remains a free-form JSON object.

## Related Documentation

- User-facing walkthrough: `docs/user_docs/api/sending-data.md`.
- Payload-shape primer: `docs/user_docs/api/data-shapes.md`.
- API how-to for customers: `docs/dev_docs/how-to/use-workflow.md`.

Keep this page synchronized with `validibot/workflows/request_utils.py` and `WorkflowViewSet`. Any time we add a new mode, change detection logic, or tweak serializer inputs, update this doc (and link to the relevant ADR if one exists).
