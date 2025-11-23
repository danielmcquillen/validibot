# ADR: Per-Workflow Submission Retention and Ephemeral Handling

- **Date:** 2025-11-23
- **Status:** Proposed
- **Owners:** Validations team
- **Related ADRs:** 2025-11-11-submission-file-type, 2025-11-20-fmi-storage-and-security-review, 2025-11-16-CEL-implementation

## Context

We currently persist user submissions by default (FileField backed by S3). That increases blast radius for privacy and compliance. Product asks for per-workflow retention controls that default to *not* storing submissions. Authors must choose:

- Not saved (ephemeral for the run only)
- Saved for 10 days
- Saved for 30 days

Regardless of retention, we must:

- Hash every submission payload and return the hash in validation status responses.
- Supply validators with content even when not persisted to S3.
- Delete ephemeral payloads as soon as a validation run concludes (success or fail).
- Keep behavior consistent across validators (including FMI/EnergyPlus that may pull content).

## Decision

Introduce a per-workflow `DataRetention` policy and route submission handling accordingly:

- **Retention policy (Workflow-level):** Add `data_retention` choice field (`DO_NOT_STORE`, `STORE_10_DAYS`, `STORE_30_DAYS`; default `DO_NOT_STORE`).
- **Hashing:** Compute SHA-256 over the raw submitted bytes for every submission, persist the hash on the submission/run record, and surface it in status/read APIs and UI. Never log raw content.
- **Ephemeral path (DO_NOT_STORE):**
  - Store submission bytes in Redis under a namespaced key (e.g., `validation:submission:{submission_id}`) with TTL covering max expected run duration plus buffer.
  - Skip FileField/S3 writes entirely.
  - Django-side executors read from Redis; Modal.com runners do not need Redis access because Django sends payloads over the wire as part of the engine request.
  - On completion (success/fail) delete the key (TTL is a fallback); allow TTL extension for long-running jobs.
  - Status responses include `content_hash` and do not reference S3/file URLs.
- **Persistent path (STORE_10_DAYS / STORE_30_DAYS):**
  - Keep existing FileField/S3 storage.
  - Persist `expires_at` based on retention window.
  - Status responses include `content_hash` and existing file references.
  - Background job enforces deletion after the window (belt-and-suspenders with S3 lifecycle if configured).
- **Backwards compatibility:** Existing workflows default to DO_NOT_STORE after migration; authors must opt into persistence.

## Options Considered

- **A) Always store in S3, add a flag to hide references.** Rejected: still retains sensitive data and violates “do not store”.
- **B) Allow authors to choose full persistence, Redis-only, or bring-your-own storage.** Too much complexity; Redis-only meets requirements and keeps data path simple.
- **C) Encrypt-everything-in-S3 with short TTL.** Still stores data and adds KMS/key-rotation overhead without eliminating data at rest.

## Scope of Changes

- **Models:** Add `data_retention` to `Workflow`; add `content_hash` and `expires_at` (for stored submissions) to the submission/run model; ensure validators can proceed without a stored file.
- **Forms/UI:** Workflow create/update forms expose retention selector (default “Not saved”). Status/detail views show hash + retention policy; never raw content for “Not saved”.
- **Views/Services:** Submission intake computes hash; branches to Redis or S3 depending on retention. Completion hooks delete Redis entry for ephemeral runs.
- **Executors:** Consumers that load submission content must check Redis first when the workflow retention is DO_NOT_STORE. For persistent paths, continue current S3 reads.
- **APIs:** Include `content_hash` in run status/read endpoints. Omit S3 URLs for ephemeral runs.
- **Background jobs:** Add periodic cleanup for:
  - Redis keys (safety sweep; TTL should handle most cases).
  - S3 objects past `expires_at` (10/30 days) plus DB record clean-up.
- **Configuration:** New settings for Redis namespace and TTL (e.g., `VALIDATION_EPHEMERAL_TTL_SECONDS`), retention windows (10d/30d), and optional S3 lifecycle integration.

## Detailed Implications

- **Hashing:**
  - Use SHA-256 over the exact byte stream; stream hashing for large uploads to avoid memory spikes.
  - Persist hash even when payload is stored; always return hash in user-facing status.
  - Use hash-only in logs/metrics to avoid leaking payloads.
- **Redis path:**
  - Requires Redis availability along validator execution path. Fail the run with a clear error if the key is missing/expired.
  - Namespace keys, limit max payload size; enforce upload size guards. If above a configured cap, require a stored retention option or reject.
  - Use `SETEX` with TTL = `max_run_time_buffered` (e.g., max expected validation runtime + 15 minutes); expose a TTL-extension hook for long-running validations.
  - Modal.com runners (FMI/EnergyPlus) do not fetch from Redis; Django must pull from Redis, then send bytes to Modal in the runner payload.
- **S3 path:**
  - Continue FileField writes via boto. Set `expires_at` and schedule deletion; configure S3 lifecycle rules on the submissions prefix for 10-day and 30-day expiration (e.g., distinct prefixes or tagged objects), with abort-incomplete-multipart uploads enabled and public access blocked.
  - Require server-side encryption (SSE-S3 or SSE-KMS) and least-privilege IAM: upload roles limited to `s3:PutObject`/`s3:DeleteObject`/`s3:GetObject` on the submissions prefix; deny `s3:ListBucket` outside the prefix; enforce `aws:SecureTransport`.
  - Rotate IAM access keys periodically (per org policy; monthly/quarterly) and prefer instance/role credentials over static keys. Enable S3 access logging for the submissions prefix. In boto uploads, set `ExtraArgs={"ServerSideEncryption": "AES256"}` (or `SSEKMSKeyId` if KMS).
  - Consider KMS key rotation (annual, automatic) if SSE-KMS is used; keep CMK aliases stable. Document key alias and rotation cadence in ops runbooks.
  - Ensure downstream tasks that assumed perpetual availability now handle 404 gracefully after expiry.
- **Executors/validators:**
  - FMI/EnergyPlus or other engines that fetch submission content must branch on retention: Redis fetch for ephemeral, existing fetch for stored. If content is absent, fail with actionable messaging.
  - CEL assertions already operate on parsed content; ensure parsing services can accept file-like objects from Redis bytes.
- **S3 setup checklist (limited persistence):**
  - Dedicated submissions prefix; lifecycle rules: Expiration 10 days for `submissions/10d/`, 30 days for `submissions/30d/`; AbortIncompleteMultipartUploads after 7 days.
  - Bucket policy: block public access; require TLS (`aws:SecureTransport`); restrict principals to app roles; scope to prefixes.
  - Encryption: SSE-S3 as baseline; SSE-KMS for tighter controls; enable automatic CMK rotation; record CMK alias in settings.
  - Credentials: favor IAM roles/instance profiles; if static keys, store in env, rotate on a fixed cadence, and monitor access logs.
  - boto settings: region-correct clients, signature v4, `ExtraArgs` for SSE, and short-lived presigned URLs if exposed (avoid exposing for ephemeral runs).
- **API/UX:**
  - Status/Run detail: show `content_hash`, retention choice, and whether payload is stored or ephemeral; never expose raw content when ephemeral.
  - Launch forms unchanged; authors pick retention during workflow authoring, not at launch time.
- **Testing:**
  - Unit: hash always present; DO_NOT_STORE skips FileField; TTL set; cleanup on completion removes Redis key.
  - Integration: launch per retention setting; validate engines can read; verify status payloads; confirm deletions at end.
  - Cleanup jobs: verify S3 deletion after window; Redis sweep removes stragglers.

## Risks and Mitigations

- **Missing Redis data (TTL too short or eviction):** Mitigate with generous TTL (buffered), eviction-resistant Redis config, and explicit delete-on-completion to keep footprint small. Surface clear errors.
- **Large payloads in Redis:** Enforce upload size limits for DO_NOT_STORE; require persistence for oversized content.
- **Validator incompatibility:** Some engines may assume file paths; provide a temporary file adapter or in-memory file-like wrapper for Redis bytes; add regression tests for FMI/EnergyPlus flows.
- **Operational drift:** S3 lifecycle rules not aligned with `expires_at`. Keep a Celery cleanup job as source of truth; treat bucket lifecycle as a safety net.
- **Privacy leaks in logs:** Audit code paths to ensure only hashes are logged. Add linters/tests for accidental content logging where feasible.
- **Throughput/load:** Redis must handle burst of uploads; estimate memory: `concurrent_runs * avg_submission_size`. For example, 100 concurrent runs with 1 MB payloads ≈ 100 MB in Redis (plus overhead). Size TTL and memory accordingly; cap upload size for ephemeral mode to protect cache.
- **Credential hygiene (S3/boto):** Use role-based creds wherever possible. If static keys are unavoidable, store in env vars (not code), rotate on schedule, and monitor `AccessDenied`/`ListBucket` anomalies via CloudWatch/S3 server access logs.

## Load and Capacity Considerations

- **Redis footprint:** `payload_size * concurrent_active_runs`. Set `VALIDATION_EPHEMERAL_MAX_SIZE_MB` to keep Redis bounded. Prefer `maxmemory-policy` that avoids evicting these keys; monitor hit/miss.
- **Hashing cost:** SHA-256 streaming is O(n); for large files, ensure chunked reads to avoid RAM spikes.
- **Cleanup churn:** Completion deletes reduce steady-state Redis usage; periodic sweep should be lightweight if TTL is set.
- **S3 churn:** Additional delete operations for 10/30-day windows; ensure Celery workers can handle periodic batch deletions without throttling S3.

## Rollout Plan

1. Schema migration: add `data_retention`, `content_hash`, `expires_at`.
2. Default existing workflows to DO_NOT_STORE.
3. Implement hashing + branching storage logic; add feature flag to gate rollout if needed.
4. Update executor paths (including FMI/EnergyPlus) to read from Redis for ephemeral runs.
5. Add cleanup jobs and settings; configure S3 lifecycle if desired.
6. UI: workflow form retention selector; status views show hash + retention.
7. Testing and smoke in lower envs with all retention modes; load-test Redis path with realistic payload sizes.

## Open Questions

- Should we allow per-launch overrides of retention, or keep it strictly per-workflow? (Current plan: workflow-level only.)
- Do we need client-visible “payload stored/ephemeral” flags in API responses beyond the hash? (Recommended for clarity.)
- Should we encrypt Redis payloads at the application layer, or rely on network-level encryption + ACLs? (Current plan: rely on TLS/ACL; optional app-level encryption could be added if required by policy.)
