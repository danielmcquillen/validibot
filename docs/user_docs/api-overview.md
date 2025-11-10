# API Overview

Give readers a quick map of the SimpleValidations REST API before they dive into implementation details.

## Base URL and Versioning
Document the production and sandbox base URLs, along with any version prefixes (for example `/api/workflows/`). Note where to find changelog information when endpoints evolve.

## Authentication
Provide a short summary of how to obtain and use API tokens, then link to the detailed Authentication guide for step-by-step instructions.

## Common Request Pattern
Explain that most endpoints expect JSON bodies, return JSON responses, and follow standard HTTP verbs. Mention that workflow run creation happens via `POST /api/workflows/<id>/start/` and requires EXECUTOR access.

## Error Format
Describe the standard error payload (`detail`, `code`, optional `status`, `type`, `errors`) so integrators know what to log or display. Include room for an example response snippet.

## Next Steps
List the follow-on pages (Sending Data, Authentication, Webhooks) so readers know where to dive deeper.
