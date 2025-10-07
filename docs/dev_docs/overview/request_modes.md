# Submission Modes

Workflow start requests arrive in three shapes. The view layer auto-detects each
mode and routes it through the appropriate parsing path.

## Mode 1 – Raw Body

* Triggered when the request body is literal content and the `Content-Type`
  header is one of the values in `SUPPORTED_CONTENT_TYPES`.
* Bypasses `ValidationRunStartSerializer`; the view reads the bytes directly
  from `request.body` and normalises them into a `Submission`.
* Optional headers:
  * `Content-Encoding: base64`
  * `X-Filename: name.ext`

This is the lightest-weight option and is used by the CLI and other services
that can control request headers.

## Mode 2 – JSON Envelope

* Request `Content-Type` must be `application/json`.
* Body is a JSON object containing a `content` field plus metadata. Example:

```json
{
  "content": "<root><value>1</value></root>",
  "content_type": "application/xml",
  "filename": "sample.xml",
  "metadata": {"source": "api"}
}
```

* Parsed by `ValidationRunStartSerializer`.
* If `content` is a Python list or dict (e.g. `{ "example": true }`), the
  serializer now coerces it to a JSON string before creating the submission.
* Optional `content_encoding` of `base64` is decoded server-side.

Mode 2 is the best fit when callers need to attach metadata or cannot set
custom headers.

## Mode 3 – Multipart Upload

* Request `Content-Type` is `multipart/form-data`.
* Primary part is the file (`file=@payload.ext`).
* Optional parts `filename`, `content_type`, and `metadata` (JSON string) allow
  overrides.
* Also parsed by `ValidationRunStartSerializer`.

Mode 3 is ideal for browser uploads or cases where the payload is large but the
client wants to avoid manual base64 encoding.

## Where mode detection lives

`WorkflowViewSet.start_validation` inspects the incoming request:

1. Grab headers/body via `extract_request_basics`.
2. `is_raw_body_mode(...)` returns `True` for Mode 1. In that case the view
   handles the request without touching the serializer.
3. Otherwise the view instantiates `ValidationRunStartSerializer`, which
   normalises Modes 2 and 3 into `validated_data` containing exactly one of
   `file` or `normalized_content`.

Downstream services treat the data identically. All three modes produce a
`Submission` row plus a queued `ValidationRun` ready for execution.

## Relationship to developer docs

* API clients should follow the [Using a Workflow via the API](../how-to/use-workflow.md)
  guide for concrete examples of each mode.
* The serializer source lives in `simplevalidations/validations/serializers.py`.
* Mode detection utilities are in `simplevalidations/workflows/request_utils.py`.
