# ADR: Per-Workflow Submission Retention and Ephemeral Handling

- **Date:** 2025-11-23
- **Updated:** 2025-12-11
- **Status:** Completed
- **Owners:** Validations team
- **Related ADRs:** 2025-11-11-submission-file-type, 2025-11-20-fmi-storage-and-security-review, 2025-11-16-CEL-implementation, 2025-11-28-pricing-system

## Context

We currently persist user submissions by default (FileField backed by GCS). That increases blast radius for privacy and compliance. Product asks for per-workflow retention controls that default to *not* storing submissions. Authors must choose:

- Not saved (ephemeral for the run only)
- Saved for 10 days
- Saved for 30 days

Regardless of retention, we must:

- Hash every submission payload and return the hash in validation status responses.
- Supply validators with content even when not persisted long-term.
- Delete ephemeral payloads as soon as a validation run concludes (success or fail).
- Keep behavior consistent across validators (including FMI/EnergyPlus that may pull content).

## Decision

Introduce a per-workflow `DataRetention` policy and route submission handling accordingly:

- **Retention policy (Workflow-level):** Add `data_retention` choice field (`DO_NOT_STORE`, `STORE_10_DAYS`, `STORE_30_DAYS`; default `DO_NOT_STORE`).
- **Hashing:** Compute SHA-256 over the raw submitted bytes for every submission, persist the hash on the submission record, and surface it in status/read APIs and UI. Never log raw content.
- **All submissions stored in database:** For this phase, all submissions are stored in the database/GCS during execution. The retention policy controls *when content is purged*, not whether it's stored initially.
- **Content purge (not record deletion):** When retention expires, we purge the content (clear `content` field, delete `input_file`) but **keep the Submission record** to preserve audit trail and avoid cascading deletes to ValidationRun records.

### Deferred: Truly Ephemeral (Memory-Only) Option

A future ADR will address truly ephemeral handling where submission content is never written to persistent storage. This would require:

- Google Cloud Memorystore (Redis/Valkey) for temporary storage
- Memory-only path during validation execution
- Additional infrastructure cost (~$35+/month minimum)

For now, even `DO_NOT_STORE` submissions are temporarily stored in the database during execution, then queued for purge as soon as the run completes (via the scheduled purge worker).

## Retention Policy Availability by Plan

Different retention options will be available based on the organization's subscription plan. This enables tiered pricing where longer retention is a premium feature.

### Data Model for Plan-Based Availability

```python
class OrgPlan(models.Model):
    """Organization subscription plan."""

    # List of retention policies available on this plan
    # E.g., ["DO_NOT_STORE"] for free tier
    # E.g., ["DO_NOT_STORE", "STORE_10_DAYS", "STORE_30_DAYS"] for premium
    available_retention_policies = ArrayField(
        models.CharField(max_length=32),
        default=list,
        help_text="Retention policies available on this plan",
    )
```

### Validation

When creating/editing a workflow:

1. Look up the organization's current plan
2. Validate that selected `data_retention` is in `plan.available_retention_policies`
3. If not available, show user-friendly error: "X-day retention requires [Plan Name]. Upgrade to enable this feature."

### Default Behavior

- Free/basic plans: Only `DO_NOT_STORE` available
- Premium plans: All options available
- If an org downgrades, existing workflows keep their settings but cannot be changed to unavailable options

## Content Purge Strategy (Safe Deletion)

### Critical Design Decision: Purge Content, Don't Delete Records

We never delete `Submission` records - we only purge the content. The record stays in the database with metadata preserved for audit trail. This avoids any cascading issues with related models.

### Defensive FK: Change ValidationRun to SET_NULL

As a defensive measure, change `ValidationRun.submission` from `CASCADE` to `SET_NULL`. This protects against accidental record deletions:

```python
# In ValidationRun model - change from CASCADE to SET_NULL
submission = models.ForeignKey(
    Submission,
    on_delete=models.SET_NULL,  # Changed from CASCADE
    null=True,
    blank=True,
    related_name="runs",
)
```

**Why SET_NULL instead of CASCADE:**
- Protects ValidationRun history if Submission is accidentally deleted
- Admin actions or cleanup scripts won't cascade unexpectedly
- ValidationRun remains queryable even if submission reference is lost
- Code that accesses `run.submission` must handle `None` case

**Code paths to update:**
- Any code assuming `run.submission` is always set must check for `None`
- API serializers should handle missing submission gracefully
- UI should show "Submission data unavailable" when `submission is None`

### Purge Content While Preserving Record

```python
class Submission(TimeStampedModel):
    # Existing fields...
    content = models.TextField(blank=True, default="")
    input_file = models.FileField(...)
    checksum_sha256 = models.CharField(...)

    # New fields for retention
    retention_policy = models.CharField(
        max_length=32,
        choices=DataRetention.choices,
        help_text="Snapshot of workflow's retention policy at submission time",
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When content should be purged (null = already purged or DO_NOT_STORE)",
    )
    content_purged_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When content was purged (for audit trail)",
    )

    def purge_content(self):
        """
        Remove submission content while preserving the record and metadata.

        Keeps: id, checksum_sha256, original_filename, size_bytes, file_type, metadata
        Clears: content, input_file
        Sets: content_purged_at
        Also cleans up: GCS execution bundle folders for all related runs
        """
        if self.content_purged_at:
            return  # Already purged (idempotent)

        # Delete file from storage
        if self.input_file:
            try:
                self.input_file.delete(save=False)
            except Exception:
                logger.exception("Failed to delete submission file", extra={"id": self.id})
                raise

        # Delete execution bundle folders for all runs
        for run in self.runs.all():
            try:
                _delete_execution_bundle(run)
            except Exception:
                logger.exception(
                    "Failed to delete execution bundle",
                    extra={"submission_id": self.id, "run_id": run.id},
                )
                # Continue with other runs - don't fail entire purge

        # Clear content
        self.content = ""
        self.input_file = None
        self.content_purged_at = timezone.now()
        self.expires_at = None  # No longer pending
        self.save(update_fields=["content", "input_file", "content_purged_at", "expires_at"])

    def get_content(self) -> str:
        """Retrieve content, handling purged submissions gracefully."""
        if self.content_purged_at:
            return ""  # Content has been purged
        # ... existing logic ...

    @property
    def is_content_available(self) -> bool:
        """Check if content is still available (not purged)."""
        return self.content_purged_at is None
```

### What's Preserved After Purge

| Field | Preserved? | Purpose |
|-------|------------|---------|
| `id` | ✅ | Record identity |
| `checksum_sha256` | ✅ | Proof of what was submitted |
| `original_filename` | ✅ | Audit trail |
| `size_bytes` | ✅ | Audit trail |
| `file_type` | ✅ | Context |
| `metadata` | ✅ | Any additional context |
| `created` | ✅ | When submitted |
| `content_purged_at` | ✅ | When purged (compliance) |
| `content` | ❌ Cleared | User data removed |
| `input_file` | ❌ Deleted | User data removed |
| Execution bundles | ❌ Deleted | GCS folders removed |

### Execution Bundle Cleanup

When validators run (FMI, EnergyPlus, etc.), files are uploaded to GCS execution bundles:

```
gs://{bucket}/runs/{org_id}/{run_id}/
    input.json          # Input envelope
    output.json         # Output envelope
    model.epjson        # Uploaded submission content
    model.fmu           # FMU file (if applicable)
    (other artifacts)
```

These folders must also be deleted when purging submission content:

```python
def _delete_execution_bundle(run: ValidationRun) -> None:
    """
    Delete the GCS execution bundle folder for a validation run.

    Uses prefix deletion to remove all objects under the run's folder.
    """
    if not run.summary:
        return

    # Get execution bundle URI from run summary (set by launcher)
    bundle_uri = run.summary.get("execution_bundle_uri")
    if not bundle_uri or not bundle_uri.startswith("gs://"):
        return

    # Parse bucket and prefix from URI
    # gs://bucket/runs/org_id/run_id/ -> bucket, runs/org_id/run_id/
    from urllib.parse import urlparse
    parsed = urlparse(bundle_uri)
    bucket_name = parsed.netloc
    prefix = parsed.path.lstrip("/")

    # Delete all objects with this prefix
    from google.cloud import storage
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blobs = bucket.list_blobs(prefix=prefix)

    for blob in blobs:
        try:
            blob.delete()
            logger.debug("Deleted GCS object: %s", blob.name)
        except Exception:
            logger.exception("Failed to delete GCS object: %s", blob.name)
            raise

    logger.info(
        "Deleted execution bundle",
        extra={"run_id": run.id, "bundle_uri": bundle_uri},
    )
```

**What gets deleted:**
- `input.json` - Input envelope with submission content references
- `output.json` - Output envelope with results
- Uploaded files (model files, weather files, etc.)
- Any artifacts produced by the validator

**What's preserved:**
- ValidationRun record in database (with summary metadata)
- ValidationFinding records
- ValidationRunSummary record

## Deletion Mechanism

### DO_NOT_STORE: Immediate Purge

For `DO_NOT_STORE` retention, purge content immediately after the validation run completes:

```python
# In validation completion handler (e.g., callback processing)
def on_validation_complete(run: ValidationRun):
    submission = run.submission
    if submission.retention_policy == DataRetention.DO_NOT_STORE:
        try:
            submission.purge_content()
            logger.info("Purged ephemeral submission", extra={"id": submission.id})
        except Exception:
            # Queue for retry - don't fail the callback
            queue_purge_retry(submission.id)
```

### Time-Bound Retention: Scheduled Purge Job

For `STORE_10_DAYS` and `STORE_30_DAYS`, a scheduled background job handles purging:

```python
# Cloud Scheduler triggers this endpoint daily (or more frequently)
@require_worker
def purge_expired_submissions():
    """
    Purge submissions past their retention window.

    Runs as a Cloud Run Job or scheduled Cloud Task.
    Processes in batches to avoid timeouts.
    """
    batch_size = 100
    max_batches = 50  # Safety limit per run

    for _ in range(max_batches):
        # Find expired submissions not yet purged
        expired = Submission.objects.filter(
            expires_at__lte=timezone.now(),
            content_purged_at__isnull=True,
        ).select_for_update(skip_locked=True)[:batch_size]

        if not expired:
            break

        for submission in expired:
            try:
                submission.purge_content()
                logger.info("Purged expired submission", extra={
                    "id": submission.id,
                    "retention": submission.retention_policy,
                    "expired_at": submission.expires_at,
                })
            except Exception:
                logger.exception("Failed to purge submission", extra={"id": submission.id})
                # Will be retried on next job run

    return {"processed": count, "remaining": Submission.objects.filter(...).count()}
```

### Job Scheduling

| Job | Frequency | Trigger |
|-----|-----------|---------|
| Purge expired submissions | Every 6 hours | Cloud Scheduler → Cloud Tasks |
| Retry failed purges | Every 1 hour | Cloud Scheduler → Cloud Tasks |
| Cleanup orphaned files | Weekly | Cloud Scheduler → Cloud Tasks |

### Setting `expires_at`

```python
# When creating a submission
def create_submission(workflow, content, ...):
    retention = workflow.data_retention

    if retention == DataRetention.DO_NOT_STORE:
        expires_at = None  # Handled by completion callback
    elif retention == DataRetention.STORE_10_DAYS:
        expires_at = timezone.now() + timedelta(days=10)
    elif retention == DataRetention.STORE_30_DAYS:
        expires_at = timezone.now() + timedelta(days=30)

    submission = Submission.objects.create(
        retention_policy=retention,
        expires_at=expires_at,
        ...
    )
```

## Error Handling

### Purge Failures

When content purge fails (e.g., GCS unavailable):

1. **Log the error** with submission ID and error details
2. **Don't fail the parent operation** (e.g., don't fail the validation callback)
3. **Queue for retry** via dedicated retry job
4. **Alert on repeated failures** (>3 failures for same submission)

### Retry Queue

```python
class PurgeRetry(models.Model):
    """Track submissions that failed to purge."""

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    last_attempt_at = models.DateTimeField(null=True)
    attempt_count = models.IntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["attempt_count", "last_attempt_at"]),
        ]
```

### Monitoring and Alerts

- **Metric:** `submissions_pending_purge` - count of expired but not-yet-purged
- **Metric:** `purge_failures_total` - count of purge errors
- **Alert:** If `submissions_pending_purge > 100` for >1 hour
- **Alert:** If same submission fails purge >3 times

## UI Requirements

### Workflow Authoring (Create/Edit)

Add retention policy selector to workflow form:

- **Field:** Dropdown with available options based on org plan
- **Label:** "Submission Data Retention"
- **Help text:** "How long to keep submitted data after validation completes"
- **Options:**
  - "Do not store (delete after validation)" - default
  - "Store for 10 days"
  - "Store for 30 days"
- **Disabled options:** Grey out options not available on current plan with upgrade prompt

### Workflow Detail (Read-Only)

Display retention policy in workflow detail views:

- Show selected retention policy
- For public workflows, clearly indicate data handling

### Submission/Run Detail Views

- Show `content_hash` (always available)
- Show retention policy that was in effect
- If content purged: "Content removed on [date] per retention policy"
- If content available: Normal content display

### API Responses

```json
{
  "submission": {
    "id": "uuid",
    "content_hash": "sha256:abc123...",
    "retention_policy": "STORE_10_DAYS",
    "content_available": true,
    "content_purged_at": null,
    "expires_at": "2025-01-20T00:00:00Z"
  }
}
```

After purge:

```json
{
  "submission": {
    "id": "uuid",
    "content_hash": "sha256:abc123...",
    "retention_policy": "STORE_10_DAYS",
    "content_available": false,
    "content_purged_at": "2025-01-20T06:15:00Z",
    "expires_at": null
  }
}
```

## Secondary Effects and Safety

### Cascading Relationships

| Model | Relationship | Impact of Purge |
|-------|--------------|-----------------|
| `ValidationRun` | FK to Submission (SET_NULL) | **Safe:** Submission reference set to NULL if record deleted |
| `ValidationStepRun` | FK to ValidationRun | **Safe:** No direct link to Submission |
| `ValidationFinding` | FK to ValidationRun | **Safe:** No direct link to Submission |
| `Submission.latest_run` | OneToOne to ValidationRun | **Safe:** Reference preserved |
| GCS execution bundles | Referenced in run.summary | **Deleted:** All files in bundle folder removed |

### Code Paths That Access Submission Content

All code that calls `submission.get_content()` must handle the purged case:

1. **Validation engines:** Content needed at execution time only - safe if purge happens after completion
2. **API endpoints:** Return appropriate error if content requested but purged
3. **Export/download features:** Check `is_content_available` before attempting
4. **Re-run features:** Cannot re-run if content purged - show clear message

### GCS/Storage Cleanup

When `input_file.delete()` is called:

- Django's storage backend handles the actual GCS delete
- If GCS delete fails, the model save is rolled back
- Orphaned files (if any) cleaned up by weekly job

### Database Constraint

Add constraint to ensure purged submissions have no content:

```python
models.CheckConstraint(
    name="submission_purged_content_cleared",
    condition=(
        Q(content_purged_at__isnull=True) |  # Not purged, or
        (Q(content="") & Q(input_file=""))    # Purged and content cleared
    ),
)
```

## Migration Strategy

### Existing Submissions

1. **Backfill `retention_policy`:** Set to `STORE_30_DAYS` for all existing submissions (preserve current behavior)
2. **Backfill `expires_at`:** Set to `created + 30 days` for existing submissions
3. **No content purge:** Existing submissions keep their content unless past the backfilled expiry

### Existing Workflows

1. **Default to `DO_NOT_STORE`:** New behavior for new submissions
2. **Communicate change:** Notify users that new submissions won't be stored by default
3. **Opt-in for retention:** Authors must explicitly choose retention if needed

## Testing Strategy

### Unit Tests

- `purge_content()` clears content and file, sets timestamp
- `purge_content()` is idempotent (safe to call twice)
- `purge_content()` deletes execution bundle from GCS
- `get_content()` returns empty string after purge
- `is_content_available` returns False after purge
- Retention policy validation respects plan availability
- `run.submission` can be None (SET_NULL) - code handles gracefully

### Integration Tests

- DO_NOT_STORE submission queued for purge when run completes
- STORE_10_DAYS submission not purged before expiry
- STORE_10_DAYS submission purged by scheduled job after expiry
- Failed purge queued for retry
- API responses reflect purge state correctly
- GCS execution bundle deleted when submission purged
- ValidationRun preserved when Submission record deleted (SET_NULL)

### Load Tests

- Scheduled purge job handles large batches
- Concurrent purge operations don't conflict
- GCS bulk deletion doesn't hit rate limits

## Open Questions

1. ~~Should we allow per-launch overrides of retention, or keep it strictly per-workflow?~~ **Resolved:** Workflow-level only.
2. ~~Do we need client-visible "payload stored/ephemeral" flags in API responses beyond the hash?~~ **Resolved:** Yes, include `content_available` and `content_purged_at`.
3. **Should we support user-initiated early deletion?** E.g., "Delete my submission now" even if retention hasn't expired. (Defer to GDPR/privacy ADR if needed.)
4. **Should we allow extending retention?** E.g., user requests to keep submission longer. (Defer - adds complexity.)

## Success Criteria

- [x] Workflows can specify retention policy
- [ ] Retention options respect org plan availability *(deferred to pricing ADR)*
- [x] Submissions correctly set `expires_at` based on retention
- [x] DO_NOT_STORE submissions queued for purge after run completion
- [x] Scheduled job purges expired submissions
- [x] Purge failures are retried automatically
- [x] UI shows retention policy in workflow forms
- [x] API responses include content availability status
- [x] GCS execution bundles deleted when submission purged
- [x] ValidationRun.submission changed to SET_NULL
- [x] Code handles `run.submission is None` gracefully
- [x] All existing tests pass
- [x] No cascading deletes of ValidationRun records
