# ADR-2025-11-27: Idempotency Keys for API Validation Requests

**Status:** Implemented (2025-12-09)  
**Owners:** Platform / API  
**Related ADRs:** 2025-12-09 Callback Idempotency, 2025-12-04 Validator Job Interface  
**Related docs:** `core/idempotency.py`, `workflows/views.py`, `docs/dev_docs/how-to/use-workflow.md`

---

## Context

Launching validations can be expensive (EnergyPlus, AI assistance) and counts against org quotas. When `POST /api/workflows/{id}/start/` times out or is retried, duplicate runs pollute findings and billing. We need the Stripe-style “send a unique key, get the original response back” behaviour. Keys stay optional for now so existing clients aren’t broken, but we expect them on mutating calls to avoid duplicate work.

## Decision

- **Header**: `Idempotency-Key` (UUIDv4 recommended, max 255 chars).
- **Scope**: `(organization_id, endpoint)`. Duplicate keys across orgs are allowed.
- **Coverage**: Implemented for `POST /api/workflows/{id}/start/` (the only mutating API today). Future mutating endpoints should also be decorated.
- **Optional**: No header → we process normally, but retries can create duplicate runs.
- **In-flight behaviour**: If the same key arrives while the first request is still running, we return `409 Conflict` (we do not block/wait).
- **Replay behaviour**: If the key completed, we return the cached status/body and set `Idempotent-Replayed: true` and `Original-Request-Id`.
- **Hash guard**: Same key with a different body returns `422 Unprocessable Entity`.
- **TTL & cleanup**: Keys expire after 24 hours (setting `IDEMPOTENCY_KEY_TTL_HOURS`); cleanup via `cleanup_idempotency_keys` management command (schedule it in ops, no Celery dependency).

Implementation landed in `core/idempotency.py` (decorator/mixin), `core/models.py::IdempotencyKey`, with tests under `core/tests/test_idempotency.py`. `WorkflowViewSet.start_validation` is decorated with `@idempotent`.

**Policy going forward:** Every new mutating API endpoint must be wrapped in the idempotency decorator before shipping. Keep the header optional for now, but always document examples with `Idempotency-Key` so clients adopt the pattern.

## Consequences

- **Positive**: Safe retries, cleaner billing/analytics, clearer debugging via `Original-Request-Id`, foundation for webhook/callback dedupe (see ADR-2025-12-09).
- **Negative**: Extra table writes; clients must manage a UUID per request; we now store response bodies for 24h.
- **Neutral**: Header remains optional; migration path exists to make it required later.

## Rollout and Next Steps

- [x] Update API examples to always show `Idempotency-Key` and note the replay headers. (Docs updated in `docs/dev_docs/how-to/use-workflow.md`.)
- [ ] If new mutating endpoints are added, wrap them with `@idempotent` and extend the tests.
- [x] Scheduled cleanup via Cloud Scheduler. Run `just gcp-scheduler-setup` to create the `validibot-cleanup-idempotency-keys` job (daily at 3 AM). See [docs/dev_docs/google_cloud/scheduled-jobs.md](../docs/dev_docs/google_cloud/scheduled-jobs.md).
- [ ] Revisit "required header" once clients have adopted the pattern.

## References

- Stripe: Idempotent Requests  
- Brandur: Implementing Stripe-like Idempotency Keys in Postgres  
- Google Cloud: Designing for Idempotency  
- AWS: Ensuring idempotency
