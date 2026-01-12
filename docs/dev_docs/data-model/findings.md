# Validation Findings

A **Finding** is a normalized issue discovered during validation.  
Findings belong to a specific Step Run.

Each finding includes:

- Severity (`success`, `info`, `warning`, `error`).
- A code (e.g. `json.schema.required`, `assertion_passed`).
- A human-readable message.
- A path or location (e.g. JSON Pointer, XPath).
- Additional metadata (e.g. line number, variable name).

## Severity levels

| Severity | Description | Badge Class |
|----------|-------------|-------------|
| `SUCCESS` | Assertion passed (positive feedback) | `text-bg-success` (green) |
| `INFO` | Informational message from validator | `text-bg-secondary` (gray) |
| `WARNING` | Non-blocking issue that should be reviewed | `text-bg-warning` (yellow) |
| `ERROR` | Blocking issue that fails validation | `text-bg-danger` (red) |

SUCCESS findings are created when assertions pass and either have a custom `success_message` or
the step has `show_success_messages=True`. See the [assertions documentation](assertions.md#success-messages)
for details.

Findings are stored separately to allow efficient filtering, pagination, and aggregation.
