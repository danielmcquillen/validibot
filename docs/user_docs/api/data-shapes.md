# Understanding Data Shapes & Types

Before you post to the Validibot API, decide how your payload should be framed. The validators that power each workflow expect specific shapes (JSON objects, XML documents, tabular text, binary archives, etc.), and the API relies on your `Content-Type` header to treat the bytes correctly. Use this guide to map your source data to the right MIME type and learn the normalization rules the platform applies.

## Common Data Sources

Most workflows fall into one of the following categories:

- **Structured JSON** coming from product catalogs, energy dashboards, or policy checklists.
- **XML or IDF** files emitted by energy-modeling tools such as EnergyPlus.
- **Delimited text** (CSV/TSV) exported from spreadsheets.
- **Plain text logs or prose** that need keyword or policy scans.
- **Binary design assets** (PDFs, ZIPs) that validators treat as opaque attachments before branching into downstream tools.

If your workflow mixes types (for example, a ZIP that contains JSON manifests), upload the outer file and let the validator unpack it. The workflow author controls how that archive is inspected.

## JSON Documents

JSON is the most common choice because Basic Assertions, JSON Schema validators, and custom policy engines all understand key paths.

- Send UTF-8 encoded text and set `Content-Type: application/json`.
- Nested fields are addressed with dot notation and `[index]` syntax (e.g., `data.error[0].message`), matching what you see in the **Edit Assertion** dialog inside the app.
- Use consistent number formats. If you send numeric strings, enable “Coerce numeric strings” in your workflow assertions or normalize the values before upload.
- Large arrays are supported, but consider trimming fields that validators ignore to reduce payload size.

## XML, IDF, and Other Markup

XML-based validators (including EnergyPlus IDF checks) expect well-formed documents.

- Send files as `application/xml` (or `text/xml`) for general XML, or `text/plain` when posting EnergyPlus `.idf` content.
- Preserve namespaces; stripping them can cause assertions to miss elements.
- Compress extra whitespace only if you are certain validators do not rely on formatting cues.

## Delimited or Plain Text Files

CSV/TSV exports and human-readable logs should be treated as text.

- Set `Content-Type: text/csv` for comma-delimited data or `text/plain` for free-form text.
- Quote fields that contain commas or newlines so downstream parsers keep rows aligned.
- When the workflow uses regex-based validators, keep encoding consistent (UTF-8) to avoid mismatched character classes.

## Binary Attachments and Archives

Some workflows expect binary files (PDF certificates, ZIPs containing BIM models, etc.).

- Use the detected MIME type if your client can calculate it; otherwise fall back to `application/octet-stream`.
- ZIP archives should include manifest files that validators can locate deterministically (for example, `manifest.json` at the root).
- If the workflow author documents checksum requirements, compute and pass the hash via metadata so reviewers can verify integrity.

## Metadata and Context

No matter which payload shape you choose, you can attach metadata alongside the file to track source systems, user IDs, or run labels.

- JSON Envelope and multipart modes accept a `metadata` field that must itself be valid JSON.
- Keep identifiers stable so assertions or automations can reference them (for example, `"claim_id": "C-1024"`).
- Avoid embedding sensitive secrets in metadata; it is stored with the submission record.

## Choosing the Correct MIME Type

| Source Format | Recommended `Content-Type` | Notes |
| ------------- | --------------------------- | ----- |
| API-generated JSON | `application/json` | Ensure UTF-8 encoding and valid JSON syntax. |
| EnergyPlus IDF | `text/plain` | File extension `.idf`; validator inspects EnergyPlus objects. |
| Generic XML | `application/xml` | Include declaration (`<?xml version="1.0"?>`) when possible. |
| CSV/TSV export | `text/csv` (or `text/tab-separated-values`) | Quote fields containing commas or tabs. |
| Plain text log | `text/plain` | Best for keyword or regex validators. |
| PDF drawings | `application/pdf` | Binary; cannot be sent via JSON envelope without base64 encoding. |
| ZIP archive | `application/zip` | Maintain consistent folder structure for validators. |

Pick the MIME type that most accurately reflects the outermost payload. Downstream validators can still transform or unpack the content, but starting with the right header ensures the API stores and streams the bytes correctly.

## Next Steps

Once you know the data shape and MIME type, proceed to [Sending Data to the API](sending-data.md) for detailed instructions on request modes, headers, and example curl commands.
