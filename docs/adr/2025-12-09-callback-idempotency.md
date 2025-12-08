# ADR-2025-12-09: Callback Idempotency for Cloud Run Jobs

**Status:** Accepted (2025-12-09) — implemented
**Owners:** Platform / Validations
**Related ADRs:** 2025-11-27-idempotency-keys (API-level idempotency)
**Related docs:** `validations/api/callbacks.py`, `validations/services/cloud_run/launcher.py`

---

## Context

### The Problem

When a Cloud Run Job completes validation, it POSTs a callback to our worker service with the results. Cloud Run has built-in retry logic: if the callback fails (timeout, 5xx error, network issue), it will retry delivery.

Without idempotency handling, retried callbacks cause:

1. **Duplicate findings** — The same validation messages get inserted multiple times
2. **Incorrect status transitions** — A run might be marked "completed" twice
3. **Corrupted summaries** — Finding counts become inaccurate
4. **Wasted processing** — Expensive operations (GCS downloads, DB writes) run multiple times

### Relationship to API Idempotency

This ADR complements ADR-2025-11-27 (API Idempotency Keys):

| Concern | API Idempotency | Callback Idempotency |
|---------|-----------------|---------------------|
| **Direction** | Client → API | Cloud Run Job → Worker |
| **Problem** | Duplicate validation runs | Duplicate findings/status |
| **Who generates ID** | API client | Launcher (at job start) |
| **What's protected** | Run creation | Callback processing |

Both are needed for a robust system.

### Why a callback_id when we already have run_id?

- The run ID identifies the resource; it does not distinguish between delivery attempts. Cloud Run will retry the same callback with the same run ID, so without a callback-specific token we would happily reapply findings and status on every retry.
- A run may have multiple legitimate callbacks across its lifecycle (e.g., per step, retries after a failed attempt). If we key solely on run ID, we either double-process retries or accidentally drop later, valid callbacks.
- The callback ID is a message ID for a single job execution; the run ID is the resource ID. We dedupe on callback ID so retries are ignored, while new callbacks for the same run can still flow through.

---

## Decision

We implement callback idempotency using a `callback_id` that flows through the job lifecycle:

### 1. Callback ID Generation

At job launch, the launcher generates a UUID `callback_id` and embeds it in the input envelope:

```python
# launcher.py
callback_id = str(uuid.uuid4())

envelope = build_energyplus_input_envelope(
    ...
    callback_id=callback_id,
    ...
)
```

### 2. Envelope Schema

The `callback_id` is part of `ExecutionContext`:

```python
# vb_shared/validations/envelopes.py
class ExecutionContext(BaseModel):
    callback_id: str | None = Field(
        default=None,
        description="Unique identifier for idempotent callback processing.",
    )
    callback_url: HttpUrl | None
    ...
```

### 3. Callback Payload Echo

The Cloud Run Job echoes the `callback_id` back in its callback:

```python
class ValidationCallback(BaseModel):
    run_id: str
    callback_id: str | None  # Echoed from input envelope
    status: ValidationStatus
    result_uri: str
```

### 4. Receipt Tracking

A `CallbackReceipt` model tracks processed callbacks:

```python
class CallbackReceipt(models.Model):
    id = models.UUIDField(primary_key=True)
    callback_id = models.CharField(max_length=255, unique=True)
    validation_run = models.ForeignKey(ValidationRun, ...)
    received_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=50)
    result_uri = models.CharField(max_length=500)
```

### 5. Handler Logic

The callback handler fetches the run first (to ensure clean 404s for invalid run IDs),
then reserves the receipt using `get_or_create` to atomically fence duplicate callbacks.
This prevents race conditions where two concurrent callbacks both pass a "no receipt exists" check:

```python
def post(self, request):
    callback = ValidationCallback.model_validate(request.data)

    # Fetch run FIRST - ensures clean 404 if run doesn't exist,
    # rather than FK error when creating the receipt.
    try:
        run = ValidationRun.objects.get(id=callback.run_id)
    except ValidationRun.DoesNotExist:
        return Response({"error": "Validation run not found"}, status=404)

    # Reserve receipt upfront to fence duplicates atomically
    receipt_created = False
    if callback.callback_id:
        with transaction.atomic():
            receipt, receipt_created = CallbackReceipt.objects.get_or_create(
                callback_id=callback.callback_id,
                defaults={
                    "validation_run": run,  # Use actual run object
                    "status": "processing",  # Placeholder until complete
                    "result_uri": callback.result_uri or "",
                },
            )
        if not receipt_created:
            # Receipt already exists - this is a duplicate
            return Response({
                "message": "Callback already processed",
                "idempotent_replayed": True,
                "original_received_at": receipt.received_at.isoformat(),
            })

    # ... process callback normally ...

    # Update receipt status after successful processing
    if callback.callback_id and receipt_created:
        receipt.status = callback.status.value
        receipt.save(update_fields=["status"])
```

This approach ensures that even if two callbacks arrive simultaneously:
1. Only one will successfully create the receipt (the loser sees `receipt_created=False`)
2. The loser returns immediately without processing
3. No duplicate findings or status updates occur

---

## Flow Diagram

```
┌─────────────┐
│   Launcher  │
│ (Django)    │
└──────┬──────┘
       │ 1. Generate callback_id = UUID
       │ 2. Build envelope with callback_id
       │ 3. Upload to GCS
       │ 4. Trigger Cloud Run Job
       ▼
┌─────────────┐
│  Cloud Run  │
│    Job      │
└──────┬──────┘
       │ 5. Read envelope from GCS
       │ 6. Run validation
       │ 7. Upload output to GCS
       │ 8. POST callback with callback_id
       ▼
┌─────────────┐     ┌──────────────────────────┐
│   Worker    │────►│ CallbackReceipt exists?  │
│  Callback   │     └──────────────────────────┘
│  Handler    │              │
└─────────────┘         Yes  │  No
                             │
                    ┌────────┴────────┐
                    ▼                 ▼
           ┌───────────────┐  ┌───────────────┐
           │ Return 200 OK │  │ Process       │
           │ (replayed)    │  │ callback      │
           └───────────────┘  └───────┬───────┘
                                      │
                                      ▼
                              ┌───────────────┐
                              │ Create        │
                              │ CallbackReceipt│
                              └───────────────┘
```

---

## Consequences

### Positive

1. **No duplicate findings** — Same callback processed only once
2. **Accurate status** — Run status reflects actual completion
3. **Correct summaries** — Finding counts remain accurate
4. **Safe retries** — Cloud Run can retry freely without side effects
5. **Debuggable** — Receipts provide audit trail of callback deliveries

### Negative

1. **Storage overhead** — One row per callback (mitigated by cleanup job)
2. **Extra DB query** — Receipt lookup on every callback (indexed, fast)
3. **Cloud Run job changes** — Job code must echo `callback_id`

### Neutral

1. **Backwards compatible** — Callbacks without `callback_id` still work
2. **No blocking** — Unlike API idempotency, no "in-progress" state needed

---

## Implementation Checklist

- [x] Add `callback_id` to `ExecutionContext` in vb_shared
- [x] Add `callback_id` to `ValidationCallback` in vb_shared
- [x] Create `CallbackReceipt` model
- [x] Generate `callback_id` in EnergyPlus launcher
- [x] Generate `callback_id` in FMI launcher
- [x] Add idempotency check to callback handler
- [x] Create receipt after successful processing
- [x] Add database migration
- [x] Write tests
- [x] Update Cloud Run job code to echo `callback_id`
- [x] Add receipt cleanup management command (`cleanup_callback_receipts`)

---

## Future Considerations

1. **Receipt cleanup** — Add a scheduled job to delete receipts older than 30 days
2. **Metrics** — Track duplicate callback rate for observability
3. **Correlation IDs** — Consider adding a `run_correlation_id` for end-to-end tracing

---

## References

- [Google Cloud: Idempotent Cloud Functions](https://cloud.google.com/blog/products/serverless/cloud-functions-pro-tips-building-idempotent-functions)
- [Cloud Run Jobs: Retries](https://cloud.google.com/run/docs/configuring/jobs#retries)
- ADR-2025-11-27: Idempotency Keys for API Validation Requests
