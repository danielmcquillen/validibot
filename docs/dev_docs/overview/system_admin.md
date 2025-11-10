# System Administration Guide

Only the system administrator (the person with access to Django admin) can edit the platform-wide Site Settings that govern API behavior, metadata policies, and future feature toggles. Organization admins do **not** see these controls.

## Site Settings Overview

- Navigate to **Django admin → Core → Site settings**.
- The project creates a single row (slug `default`). Leave the slug alone; edit the JSON field to adjust configuration.
- When fields are missing or invalid, the application falls back to safe defaults and rewrites the stored JSON so everything stays normalized.

All settings are loaded into typed Pydantic models (`simplevalidations/core/site_settings.py`), so consider this file the source of truth for available options.

## API Submission Policies

Two knobs currently ship for managing workflow start requests:

| Setting | Default | Purpose |
| ------- | ------- | ------- |
| `metadata_key_value_only` | `false` | When `true`, metadata submitted in Modes 2 and 3 must be a flat dictionary of scalar values (strings/numbers/bools/null). Nested lists or dicts trigger a `400 INVALID_PAYLOAD` response. |
| `metadata_max_bytes` | `4096` | Maximum size (UTF-8 bytes) for stored metadata after the system adds derived keys (like `sha256`). Set to `0` to disable the limit. |

These settings apply to both JSON envelopes and multipart submissions. Raw-body Mode 1 currently ignores metadata because it does not accept metadata input.

### Operational Guidance

1. **Rolling out stricter policies**: Enable `metadata_key_value_only` before onboarding new integrations. Existing clients should be notified because nested metadata will start failing immediately.
2. **Choosing a byte limit**: 4 KB covers typical ID/label pairs. Increase the limit temporarily if a partner needs larger metadata, but consider capturing that extra context elsewhere (e.g., inside the submission payload).
3. **Monitoring**: Metadata violations return a structured API error (`code: INVALID_PAYLOAD`, `field: "metadata"`). Track these responses in your API analytics to verify when clients hit policy ceilings.

As more platform-wide controls emerge (rate limits, streaming thresholds, etc.), add them to the Site Settings JSON and extend the Pydantic models so the rest of the application inherits defaults automatically.
