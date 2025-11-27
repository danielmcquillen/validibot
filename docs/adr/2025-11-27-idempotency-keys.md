# ADR-2025-11-27: Idempotency Keys for API Validation Requests

**Status:** Proposed (2025-11-27)  
**Owners:** Platform / API  
**Related ADRs:** —  
**Related docs:** `config/api_router.py`, `workflows/views.py`

---

## Context

### The Problem

When a client calls `POST /api/workflows/{id}/start/` to launch a validation run, network issues can cause uncertainty:

1. **Timeout before response** – The client sends the request, the server processes it, but the response is lost. The client doesn't know if the run was created.
2. **Retry storms** – The client retries, creating duplicate validation runs (wasting compute, confusing users, potentially inflating billing).
3. **Webhook double-delivery** – If we later add webhook callbacks, duplicate runs mean duplicate notifications to downstream systems.

This is especially problematic for SimpleValidations because:

- Validation runs can be expensive (EnergyPlus simulations, AI-assisted analysis).
- Runs are billed against org quotas (`UsageCounter`).
- Duplicate findings pollute dashboards and analytics.

### Industry Best Practice: Idempotency Keys

The standard solution, pioneered by Stripe and adopted by most modern payment/infrastructure APIs, is **idempotency keys**:

> An idempotency key is a unique identifier that the client generates and sends with a mutating request. If the server receives a second request with the same key, it returns the original response instead of processing the request again.

#### How It Works (Stripe Model)

1. **Client generates a UUID** (e.g., `Idempotency-Key: 8f14e45f-ceea-467f-a8ad-0e7e3a1a8b9c`).
2. **Server receives request**, checks if that key has been seen before for this org/user.
3. **If new**: Process the request, store the key + response, return the response.
4. **If seen before**: Return the stored response without reprocessing.
5. **Key expires** after a reasonable window (Stripe: 24 hours).

#### Benefits

| Benefit                    | Description                                                   |
| -------------------------- | ------------------------------------------------------------- |
| **Safe retries**           | Clients can retry failed requests without fear of duplication |
| **Network resilience**     | Handles timeouts, dropped connections, load balancer retries  |
| **Simplifies client code** | No need for complex "check-then-create" logic                 |
| **Audit trail**            | Keys provide correlation IDs for debugging                    |

#### Who Uses This Pattern

| Provider                | Header                     | Expiry     | Notes                                 |
| ----------------------- | -------------------------- | ---------- | ------------------------------------- |
| **Stripe**              | `Idempotency-Key`          | 24 hours   | Original implementer, well-documented |
| **PayPal**              | `PayPal-Request-Id`        | Varies     | Required for all mutating calls       |
| **Square**              | `Idempotency-Key`          | 24 hours   | Same pattern as Stripe                |
| **Adyen**               | `Idempotency-Key`          | Varies     | Payments API                          |
| **Shopify**             | `Idempotency-Key`          | 60 seconds | Shorter window                        |
| **AWS (some services)** | `X-Amzn-Idempotency-Token` | Varies     | Not universal                         |
| **Google Cloud**        | `X-Goog-Request-Id`        | Varies     | For certain APIs                      |
| **Twilio**              | Built into resource IDs    | —          | Different approach                    |

#### Key Design Decisions in the Wild

1. **Header name**: `Idempotency-Key` is the de facto standard.
2. **Key format**: UUIDv4 is typical, but any unique string works (max ~255 chars).
3. **Scope**: Keys are typically scoped to (API key / user / org) + endpoint.
4. **Expiry**: 24 hours is common; shorter for high-volume APIs.
5. **Storage**: Redis (fast, TTL built-in) or database with scheduled cleanup.
6. **Response storage**: Store the full HTTP response (status, headers, body) or just enough to reconstruct it.
7. **In-flight handling**: If a duplicate request arrives while the first is still processing, either:
   - Block until the first completes (Stripe's approach)
   - Return 409 Conflict immediately (simpler)

#### Edge Cases to Handle

| Scenario                         | Expected Behavior                      |
| -------------------------------- | -------------------------------------- |
| Same key, same request body      | Return cached response                 |
| Same key, different request body | Return 422 error (key reuse violation) |
| Key missing (optional mode)      | Process normally, no idempotency       |
| Key missing (required mode)      | Return 400 error                       |
| Request still processing         | Return 409 Conflict or block           |
| Key expired                      | Process as new request                 |

---

## Decision

We will implement idempotency keys for the `POST /api/workflows/{id}/start/` endpoint with the following design:

### 1. Header and Format

- **Header**: `Idempotency-Key` (industry standard)
- **Format**: Any string, max 255 characters (UUIDv4 recommended)
- **Required**: No (optional for MVP, can make required later)
- **Scope**: Key uniqueness is scoped to `(organization_id, endpoint)`

### 2. Data Model

```python
class IdempotencyKey(models.Model):
    """
    Stores idempotency keys to prevent duplicate API requests.

    Keys are scoped to an organization and endpoint. When a request arrives
    with a key we've seen before, we return the stored response instead of
    processing the request again.
    """

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["org", "key", "endpoint"],
                name="uq_idempotency_org_key_endpoint",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "key", "endpoint"]),
            models.Index(fields=["expires_at"]),  # For cleanup job
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    org = models.ForeignKey(
        "users.Organization",
        on_delete=models.CASCADE,
        related_name="idempotency_keys",
    )
    key = models.CharField(max_length=255, db_index=True)
    endpoint = models.CharField(max_length=100)  # e.g., "workflow_start"

    # Request fingerprint to detect key reuse with different payload
    request_hash = models.CharField(max_length=64)  # SHA256 of request body

    # Cached response
    response_status = models.SmallIntegerField()
    response_body = models.JSONField()
    response_headers = models.JSONField(default=dict, blank=True)

    # Reference to created resource (if applicable)
    validation_run = models.ForeignKey(
        "validations.ValidationRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    # Lifecycle
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()  # Default: created_at + 24 hours

    # For debugging
    request_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
```

### 3. Processing Flow

```
┌─────────────────┐
│  API Request    │
│  with header    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     No key?     ┌─────────────────┐
│  Extract        │────────────────►│  Process        │
│  Idempotency-Key│                 │  normally       │
└────────┬────────┘                 └─────────────────┘
         │ Key present
         ▼
┌─────────────────┐     Not found   ┌─────────────────┐
│  Lookup key in  │────────────────►│  Create key     │
│  IdempotencyKey │                 │  record (locked)│
└────────┬────────┘                 └────────┬────────┘
         │ Found                             │
         ▼                                   ▼
┌─────────────────┐                 ┌─────────────────┐
│  Check request  │                 │  Process        │
│  hash matches?  │                 │  request        │
└────────┬────────┘                 └────────┬────────┘
         │                                   │
    ┌────┴────┐                              │
    │         │                              │
  Match    Mismatch                          │
    │         │                              │
    ▼         ▼                              ▼
┌───────┐ ┌───────┐                 ┌─────────────────┐
│Return │ │Return │                 │  Store response │
│cached │ │422    │                 │  in key record  │
│response│ │error │                 └────────┬────────┘
└───────┘ └───────┘                          │
                                             ▼
                                    ┌─────────────────┐
                                    │  Return response│
                                    └─────────────────┘
```

### 4. Implementation Location

We'll implement this as a DRF mixin that can be applied to any ViewSet action:

```python
# simplevalidations/core/idempotency.py

class IdempotencyMixin:
    """
    Mixin for DRF views that provides idempotency key support.

    Usage:
        class MyViewSet(IdempotencyMixin, viewsets.ModelViewSet):
            idempotent_actions = ["create", "start_validation"]
    """

    idempotent_actions: list[str] = []
    idempotency_key_header = "HTTP_IDEMPOTENCY_KEY"
    idempotency_ttl_hours = 24

    def get_idempotency_key(self, request) -> str | None:
        return request.META.get(self.idempotency_key_header)

    def get_idempotency_endpoint(self) -> str:
        return f"{self.__class__.__name__}.{self.action}"
```

### 5. Response Headers

When returning a cached response, include headers to indicate this:

```http
HTTP/1.1 201 Created
Idempotent-Replayed: true
Original-Request-Id: abc123
```

### 6. Cleanup

A scheduled Celery task will delete expired keys:

```python
@app.task
def cleanup_expired_idempotency_keys():
    """Delete idempotency keys older than their expiry time."""
    IdempotencyKey.objects.filter(expires_at__lt=timezone.now()).delete()
```

Schedule: Daily (or more frequently for high-volume deployments).

### 7. Error Responses

| Scenario                       | Status | Response                                                                                                                 |
| ------------------------------ | ------ | ------------------------------------------------------------------------------------------------------------------------ |
| Key reused with different body | 422    | `{"detail": "Idempotency key has already been used with a different request body.", "code": "idempotency_key_reused"}`   |
| Key too long                   | 400    | `{"detail": "Idempotency key exceeds maximum length of 255 characters.", "code": "idempotency_key_too_long"}`            |
| Request in progress            | 409    | `{"detail": "A request with this idempotency key is currently being processed.", "code": "idempotency_key_in_progress"}` |

---

## MVP Scope

For the January alpha, we will implement a minimal version:

### In Scope

1. **Single endpoint**: `POST /api/workflows/{id}/start/` only.
2. **Optional header**: Requests without the header process normally.
3. **Database storage**: Use PostgreSQL (no Redis dependency for MVP).
4. **24-hour expiry**: Fixed TTL, configurable via settings.
5. **Basic cleanup**: Daily Celery task.
6. **Documentation**: Add to API docs with examples.

### Out of Scope (Future)

- Required idempotency keys (break existing clients)
- Redis storage for high-performance lookups
- In-flight request blocking (will return 409 for now)
- Idempotency for other endpoints (create workflow, etc.)
- Per-org configurable TTL
- Idempotency key analytics/reporting

---

## Consequences

### Positive

1. **Safer API usage** – Clients can retry without fear of duplicate runs.
2. **Better UX** – Network issues don't result in confusing duplicate data.
3. **Accurate billing** – Quota usage reflects actual work, not retries.
4. **Industry standard** – Developers familiar with Stripe/PayPal will recognize the pattern.
5. **Foundation for webhooks** – When we add webhook delivery, idempotency keys help correlate events.

### Negative

1. **Storage overhead** – One row per unique request (mitigated by expiry/cleanup).
2. **Complexity** – Another concept for API consumers to understand (mitigated by being optional).
3. **Race conditions** – Concurrent duplicate requests need careful handling.

### Neutral

1. **Migration** – Existing clients unaffected (header is optional).
2. **Testing** – Need to test idempotency behavior in integration tests.

---

## Implementation Checklist

- [ ] Create `IdempotencyKey` model and migration
- [ ] Implement `IdempotencyMixin` for DRF views
- [ ] Apply mixin to `WorkflowViewSet.start_validation`
- [ ] Add `cleanup_expired_idempotency_keys` Celery task
- [ ] Add tests:
  - [ ] Duplicate request returns cached response
  - [ ] Different body with same key returns 422
  - [ ] Expired key allows reprocessing
  - [ ] Missing key processes normally
- [ ] Update API documentation
- [ ] Add `Idempotency-Key` to API reference examples

---

## References

- [Stripe: Idempotent Requests](https://stripe.com/docs/api/idempotent_requests)
- [PayPal: Idempotency](https://developer.paypal.com/api/rest/reference/idempotency/)
- [Brandur: Implementing Stripe-like Idempotency Keys in Postgres](https://brandur.org/idempotency-keys)
- [Google Cloud: Designing for Idempotency](https://cloud.google.com/blog/products/serverless/cloud-functions-pro-tips-building-idempotent-functions)
- [AWS: Ensuring idempotency](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/Run_Instance_Idempotency.html)
