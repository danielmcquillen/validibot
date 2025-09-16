# Validation Findings

A **Finding** is a normalized issue discovered during validation.  
Findings belong to a specific Step Run.

Each finding includes:

- Severity (`info`, `warning`, `error`).
- A code (e.g. `json.schema.required`).
- A human-readable message.
- A path or location (e.g. JSON Pointer, XPath).
- Additional metadata (e.g. line number, variable name).

Findings are stored separately to allow efficient filtering, pagination, and aggregation.
