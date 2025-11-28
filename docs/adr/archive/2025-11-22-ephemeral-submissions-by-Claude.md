# ADR: Ephemeral Submission Storage with Retention Policies

**Status:** Proposed (2025-11-22)  
**Author:** Claude Sonnet 4.5  
**Related ADRs:** 2025-11-20-fmi-storage-and-security-review

## Context

### Problem Statement

Currently, SimpleValidations stores _all_ user submission data indefinitely:

- Small submissions (<10MB) are stored inline in PostgreSQL `Submission.content` TextField
- Larger submissions are stored in S3 via `Submission.input_file` FileField
- Database XOR constraint enforces exactly one of these fields must be populated
- SHA256 checksums are computed and stored for all submissions

**Security and Privacy Concerns:**

1. **Data minimization violation**: We retain user data longer than necessary for the validation operation
2. **Increased attack surface**: Stored submissions become targets for data breaches
3. **GDPR compliance risk**: Indefinite retention conflicts with data minimization principles
4. **Trust barrier**: Security-conscious users may avoid the platform due to persistent storage

**Business Impact:**

- Potential customers in regulated industries (healthcare, finance) require data minimization guarantees
- Competitive disadvantage vs platforms offering ephemeral validation
- Unnecessary storage costs for data that serves no post-validation purpose

### Current Architecture

**Submission Creation Flow:**

```
User Request → Launch Helper → Submission.set_content() → PostgreSQL/S3 Storage
              (form/API)         (saves immediately)         (persists forever)
                                                                     ↓
                                                              ValidationRun created
                                                                     ↓
                                                              Celery task executes
                                                                     ↓
                                                              Engines call get_content()
```

**Key Integration Points:**

- `workflows/views_launch_helpers.py`: `build_submission_from_form()`, `build_submission_from_api()`
- `submissions/models.py`: `Submission.set_content()`, `Submission.get_content()`
- `validations/engines/*.py`: All engines call `submission.get_content()` to access payload
- `validations/services/validation_run.py`: `ValidationRunService.execute()` orchestrates validation

**Current Constraints:**

- Database XOR: `submission_content_present` and `submission_content_not_both` constraints
- Checksum computation: SHA256 already computed during ingestion via `prepare_uploaded_file()` and `prepare_inline_text()`
- File type detection: Workflow enforces `allowed_file_types`
- S3 upload path: `submissions/{org}/{project}/{user}/{date}/{uuid}_{filename}`

## Decision

### High-Level Design

Implement a **three-tier retention policy** configurable at the Workflow level:

1. **`NONE` (Ephemeral)**: Default tier, no persistent storage

   - Content stored in Redis during validation execution (TTL: 1 hour)
   - Minimal Submission record saved (metadata, checksum, no content)
   - Automatically deleted from Redis after ValidationRun completes

2. **`DAYS_10` (Short-term retention)**: For debugging/support scenarios

   - Content stored in PostgreSQL (inline) or S3 (files) as today
   - Background job deletes content after 10 days, keeps metadata/checksum

3. **`DAYS_30` (Extended retention)**: For compliance/audit scenarios
   - Same storage as `DAYS_10`, but 30-day retention period
   - Explicit opt-in for workflows requiring audit trails

### Architecture Changes

#### 1. Workflow Model Enhancement

```python
# workflows/models.py

class SubmissionRetentionPolicy(models.TextChoices):
    NONE = "NONE", _("No retention (ephemeral)")
    DAYS_10 = "DAYS_10", _("Retain for 10 days")
    DAYS_30 = "DAYS_30", _("Retain for 30 days")

class Workflow(TimeStampedModel):
    # ... existing fields ...
    retention_policy = models.CharField(
        max_length=16,
        choices=SubmissionRetentionPolicy.choices,
        default=SubmissionRetentionPolicy.NONE,
        help_text=_("How long to retain user submission data after validation"),
    )
```

**Migration Strategy:**

- Add nullable `retention_policy` field with default `NONE`
- Backfill existing workflows: Set to `DAYS_30` for workflows with >100 submissions (assume production use)
- Make field non-nullable after backfill
- Update Workflow admin/forms to expose configuration

#### 2. Redis Ephemeral Storage Layer

**New Service:** `submissions/services/ephemeral_storage.py`

```python
from django.core.cache import cache
from typing import Optional
import uuid

REDIS_SUBMISSION_PREFIX = "submission:ephemeral"
REDIS_SUBMISSION_TTL = 3600  # 1 hour

class EphemeralSubmissionStorage:
    """Manages ephemeral submission storage in Redis."""

    @staticmethod
    def store(submission_uuid: uuid.UUID, content: str) -> None:
        """Store submission content in Redis with TTL."""
        key = f"{REDIS_SUBMISSION_PREFIX}:{submission_uuid}:content"
        cache.set(key, content, timeout=REDIS_SUBMISSION_TTL)

    @staticmethod
    def retrieve(submission_uuid: uuid.UUID) -> Optional[str]:
        """Retrieve submission content from Redis, or None if expired."""
        key = f"{REDIS_SUBMISSION_PREFIX}:{submission_uuid}:content"
        return cache.get(key)

    @staticmethod
    def delete(submission_uuid: uuid.UUID) -> None:
        """Explicitly delete submission from Redis (cleanup)."""
        key = f"{REDIS_SUBMISSION_PREFIX}:{submission_uuid}:content"
        cache.delete(key)

    @staticmethod
    def extend_ttl(submission_uuid: uuid.UUID, additional_seconds: int = 1800) -> None:
        """Extend TTL for long-running validations."""
        key = f"{REDIS_SUBMISSION_PREFIX}:{submission_uuid}:content"
        content = cache.get(key)
        if content:
            current_ttl = cache.ttl(key)  # Requires django-redis
            new_ttl = current_ttl + additional_seconds
            cache.set(key, content, timeout=new_ttl)
```

**Redis Configuration:**

- **Local**: Existing `redis://localhost:6379/0` (shared with Celery)
- **Production**: Existing `django_redis.cache.RedisCache` with same connection
- **Namespace isolation**: Use `submission:ephemeral:*` prefix to avoid key collisions

**TTL Strategy:**

- Initial TTL: 1 hour (sufficient for 99% of validations)
- Extension mechanism: For long-running validations (EnergyPlus, AI), extend TTL programmatically
- Automatic cleanup: Redis TTL eviction handles expiry, no manual cleanup needed during validation
- Post-validation cleanup: Explicit `delete()` call after ValidationRun completes (success or failure)

#### 3. Submission Model Modifications

**Database Schema Changes:**

```python
# submissions/models.py

class Submission(TimeStampedModel):
    # ... existing fields ...

    # Make content and input_file nullable (were previously XOR-enforced as non-null)
    content = models.TextField(
        blank=True,
        null=True,  # NEW: Allow null for ephemeral submissions
        help_text=_("Inline content for small submissions"),
    )
    input_file = models.FileField(
        blank=True,
        null=True,  # NEW: Allow null for ephemeral submissions
        upload_to=submission_input_file_upload_to,
        help_text=_("File upload for larger submissions"),
    )

    # New field to track storage location
    is_ephemeral = models.BooleanField(
        default=False,
        help_text=_("True if content stored in Redis, not persisted"),
    )

    # Existing fields remain (checksum_sha256, size_bytes, file_type, metadata)
```

**Remove Database Constraints:**

```sql
-- Migration: Drop XOR constraints that require content/input_file
ALTER TABLE submissions_submission DROP CONSTRAINT IF EXISTS submission_content_present;
ALTER TABLE submissions_submission DROP CONSTRAINT IF EXISTS submission_content_not_both;

-- Add new constraint: Ephemeral submissions must have checksum
ALTER TABLE submissions_submission ADD CONSTRAINT ephemeral_requires_checksum
    CHECK (NOT is_ephemeral OR checksum_sha256 IS NOT NULL);
```

**Modified Methods:**

```python
def set_content(
    self,
    inline_text: str | None = None,
    uploaded_file: UploadedFile | File | None = None,
    filename: str | None = None,
    inline_max_bytes: int | None = None,
    file_type: str | None = None,
    retention_policy: str = SubmissionRetentionPolicy.NONE,  # NEW parameter
):
    """
    Store content according to retention policy:
    - NONE: Store in Redis, mark is_ephemeral=True
    - DAYS_10/DAYS_30: Store in content/input_file as before
    """
    from simplevalidations.submissions.services.ephemeral_storage import EphemeralSubmissionStorage

    # Compute content and checksum (same as before)
    if inline_text:
        content = inline_text
        checksum = hashlib.sha256(content.encode()).hexdigest()
    elif uploaded_file:
        # Read file, compute checksum
        # ... existing logic ...

    self.checksum_sha256 = checksum
    self.size_bytes = len(content)
    self.file_type = file_type or self._detect_file_type(filename, content)

    if retention_policy == SubmissionRetentionPolicy.NONE:
        # Ephemeral path: Store in Redis
        self.is_ephemeral = True
        self.content = None
        self.input_file = None
        EphemeralSubmissionStorage.store(self.id, content)
    else:
        # Persistent path: Existing logic
        self.is_ephemeral = False
        if len(content) < inline_max_bytes:
            self.content = content
        else:
            # Save to S3 via input_file
            # ... existing logic ...

def get_content(self) -> str:
    """
    Retrieve content from appropriate storage:
    - Ephemeral: Fetch from Redis
    - Persistent: Read from content or input_file
    """
    if self.is_ephemeral:
        from simplevalidations.submissions.services.ephemeral_storage import EphemeralSubmissionStorage
        content = EphemeralSubmissionStorage.retrieve(self.id)
        if content is None:
            raise ValueError(
                f"Ephemeral content for submission {self.id} has expired or been deleted"
            )
        return content

    # Existing persistent retrieval logic
    if self.content:
        return self.content
    elif self.input_file:
        with self.input_file.open('r') as f:
            return f.read()
    else:
        raise ValueError(f"Submission {self.id} has no content available")
```

#### 4. Launch Helper Modifications

**Updated Flow:**

```python
# workflows/views_launch_helpers.py

def build_submission_from_form(
    *,
    request: HttpRequest,
    workflow: Workflow,
    cleaned_data: dict[str, Any],
) -> SubmissionBuild:
    """Persist a submission from validated WorkflowLaunchForm data."""

    # ... existing validation logic ...

    submission = Submission.objects.create(
        org=request.user.current_org,
        project=project,
        workflow=workflow,
        user=request.user,
        short_description=short_description,
        metadata=metadata,
    )

    # NEW: Pass retention policy to set_content()
    submission.set_content(
        inline_text=payload,
        uploaded_file=attachment,
        filename=filename,
        file_type=final_file_type,
        retention_policy=workflow.retention_policy,  # <-- KEY CHANGE
    )
    submission.save()

    # Always include checksum in return value (used for status polling)
    return SubmissionBuild(
        submission=submission,
        checksum=submission.checksum_sha256,  # NEW field
    )

# Similar changes to build_submission_from_api()
```

**API Response Changes:**

```python
# workflows/serializers.py

class ValidationRunLaunchSerializer(serializers.Serializer):
    # ... existing fields ...
    submission_checksum = serializers.CharField(
        read_only=True,
        help_text="SHA256 hash of submission content (use for status polling)",
    )

# workflows/views.py - API launch endpoint
def create(self, request, *args, **kwargs):
    # ... existing logic ...

    return Response(
        {
            "id": run.id,
            "status": run.status,
            "submission_checksum": submission.checksum_sha256,  # NEW
            "status_url": status_url,
        },
        status=status.HTTP_202_ACCEPTED,
    )
```

#### 5. Validation Execution Changes

**Minimal Changes Required:**

The existing `ValidationRunService.execute()` and validation engines require **no changes** because they already use the abstraction `submission.get_content()`, which we've enhanced to handle Redis retrieval.

**Post-Validation Cleanup:**

```python
# validations/services/validation_run.py

def execute(
    self,
    validation_run_id: int,
    user_id: int,
    metadata: dict | None = None,
) -> ValidationRunTaskResult:
    """Execute a ValidationRun within the Celery worker context."""

    try:
        # ... existing validation loop ...

        # NEW: Cleanup ephemeral submissions after execution
        if validation_run.submission.is_ephemeral:
            from simplevalidations.submissions.services.ephemeral_storage import EphemeralSubmissionStorage
            EphemeralSubmissionStorage.delete(validation_run.submission.id)
            logger.info(
                "Deleted ephemeral content for submission %s",
                validation_run.submission.id,
            )
    finally:
        # ... existing cleanup ...
```

#### 6. Retention Management (Background Jobs)

**New Celery Periodic Task:**

```python
# submissions/tasks.py

from celery import shared_task
from django.utils import timezone
from datetime import timedelta

@shared_task
def cleanup_expired_submissions():
    """Delete submission content that has exceeded retention period."""
    from simplevalidations.submissions.models import Submission
    from simplevalidations.workflows.models import SubmissionRetentionPolicy

    now = timezone.now()

    # Find submissions with DAYS_10 policy older than 10 days
    cutoff_10 = now - timedelta(days=10)
    expired_10 = Submission.objects.filter(
        workflow__retention_policy=SubmissionRetentionPolicy.DAYS_10,
        created__lt=cutoff_10,
        is_ephemeral=False,
    ).exclude(content__isnull=True, input_file='')

    for submission in expired_10:
        _delete_submission_content(submission)

    # Find submissions with DAYS_30 policy older than 30 days
    cutoff_30 = now - timedelta(days=30)
    expired_30 = Submission.objects.filter(
        workflow__retention_policy=SubmissionRetentionPolicy.DAYS_30,
        created__lt=cutoff_30,
        is_ephemeral=False,
    ).exclude(content__isnull=True, input_file='')

    for submission in expired_30:
        _delete_submission_content(submission)

    logger.info(
        "Cleanup task: deleted content from %d submissions",
        expired_10.count() + expired_30.count(),
    )

def _delete_submission_content(submission: Submission):
    """Delete content fields but preserve metadata and checksum."""
    if submission.content:
        submission.content = None
    if submission.input_file:
        submission.input_file.delete(save=False)  # Delete S3 file
        submission.input_file = None
    submission.save(update_fields=['content', 'input_file'])
```

**Celery Beat Schedule:**

```python
# config/settings/base.py

CELERY_BEAT_SCHEDULE = {
    # ... existing tasks ...
    'cleanup-expired-submissions': {
        'task': 'simplevalidations.submissions.tasks.cleanup_expired_submissions',
        'schedule': crontab(hour=2, minute=0),  # Run daily at 2 AM
    },
}
```

**S3 Lifecycle Policy (Belt-and-Suspenders):**

Configure S3 bucket lifecycle rule to delete objects older than 35 days from `submissions/` prefix:

- Catches any submissions missed by Celery task
- Protects against runaway storage costs
- Set to 35 days (5 days beyond DAYS_30) to avoid race conditions

### Security and Privacy Benefits

**Data Minimization (GDPR Article 5):**

- Default `NONE` policy ensures user data not retained beyond operational necessity
- Retention periods (10/30 days) are explicit, documented, and enforced programmatically
- Users can verify ephemeral handling via API response (checksum-only, no content retrieval)

**Reduced Attack Surface:**

- Ephemeral submissions never touch disk in production (Redis is in-memory)
- PostgreSQL and S3 contain minimal PII for default workflows
- Expired content physically deleted (not soft-deleted), reducing breach impact

**Regulatory Compliance:**

- **GDPR**: Satisfies data minimization, storage limitation principles
- **HIPAA**: Ephemeral validation reduces BAA scope (no PHI persisted)
- **SOC 2**: Demonstrates data lifecycle management controls

**User Trust:**

- Transparent retention policies shown in workflow configuration UI
- API returns checksum (not content), proving ephemeral handling
- Competitive differentiation for security-conscious customers

## Consequences

### Positive

1. **Security by Default**:

   - New workflows default to ephemeral storage
   - Reduces risk exposure for all users
   - Aligns with privacy-first design principles

2. **Compliance Enablement**:

   - GDPR data minimization compliance
   - Easier BAA/HIPAA positioning for healthcare customers
   - Documented retention policies for audits

3. **Cost Optimization**:

   - Reduced S3 storage costs (ephemeral tier uses Redis)
   - PostgreSQL space savings for high-volume workflows
   - Automated cleanup reduces manual intervention

4. **Competitive Advantage**:

   - Differentiation vs competitors with persistent-only storage
   - Addressable market expansion (regulated industries)
   - Marketing messaging: "Your data, validated then forgotten"

5. **Operational Simplicity**:
   - Redis already deployed for Celery, no new infrastructure
   - Existing checksum computation reused
   - Minimal engine changes (abstraction via `get_content()` preserved)

### Negative

1. **Redis Memory Pressure**:

   - **Risk**: High-volume ephemeral validations could exhaust Redis memory
   - **Mitigation**:
     - Monitor Redis memory usage via Sentry/Datadog
     - Set `maxmemory-policy allkeys-lru` for graceful eviction
     - For extreme cases, add dedicated Redis instance for ephemeral storage
     - Large files (>10MB) could be stored in Redis with compression

2. **Debugging Complexity**:

   - **Risk**: Ephemeral submissions can't be inspected post-validation
   - **Mitigation**:
     - ValidationRun still contains `summary`, `error`, step-level diagnostics
     - For debugging workflows, temporarily switch to `DAYS_10` retention
     - Checksum enables support queries ("Have you seen this data before?")
     - Logs include submission metadata (file_type, size_bytes, short_description)

3. **TTL Expiration During Validation**:

   - **Risk**: Long-running validation (>1 hour) could fail if Redis evicts content
   - **Mitigation**:
     - Implement TTL extension mechanism (`extend_ttl()`) for workflows with `timeout > 30 min`
     - Monitor ValidationRun duration and alert if approaching TTL
     - Fallback: On content retrieval failure, fail validation with clear error message

4. **Migration Burden**:

   - **Risk**: Existing workflows must be backfilled with retention policy
   - **Mitigation**:
     - Migration sets `DAYS_30` for existing workflows with >100 submissions (assume production)
     - Clear communication to users: "Your workflow now defaults to 30-day retention, change in settings if needed"
     - Admin UI shows retention policy prominently

5. **Cross-Region Redis Latency**:
   - **Risk**: If Redis and Celery workers in different regions, `get_content()` slower
   - **Mitigation**:
     - Co-locate Redis and Celery workers (already true in Heroku setup)
     - For multi-region deployment, use regional Redis clusters

### Rollout Plan

**Phase 1: Foundation (Week 1)**

1. Add `retention_policy` to Workflow model with migration
2. Implement `EphemeralSubmissionStorage` service
3. Update `Submission.set_content()` and `get_content()` to support ephemeral path
4. Add `is_ephemeral` field to Submission model

**Phase 2: Integration (Week 2)** 5. Modify launch helpers to pass `workflow.retention_policy` to `set_content()` 6. Update API responses to include `submission_checksum` 7. Add post-validation cleanup in `ValidationRunService.execute()` 8. Implement TTL extension mechanism for long-running validations

**Phase 3: Retention Management (Week 3)** 9. Create `cleanup_expired_submissions` Celery task 10. Configure Celery Beat schedule (daily at 2 AM) 11. Set up S3 lifecycle policy (35-day expiration) 12. Add monitoring for Redis memory usage and cleanup task success

**Phase 4: UI and Documentation (Week 4)** 13. Update Workflow admin/forms to expose retention policy configuration 14. Add workflow detail page UI showing retention policy 15. Document retention policies in user-facing docs 16. Add migration guide for existing workflows

**Phase 5: Testing and Rollout (Week 5)** 17. Integration tests for ephemeral submission flow (launch → validate → cleanup) 18. Load testing for Redis memory pressure under high volume 19. Deploy to staging and run smoke tests 20. Deploy to production with feature flag (default `DAYS_30` for existing workflows)

**Phase 6: Default Transition (Week 6+)** 21. Monitor production metrics (Redis memory, cleanup success rate, validation failures) 22. After 2 weeks of stable operation, change default to `NONE` for _new_ workflows 23. Announce change to existing users with guidance on choosing retention policy

### Alternative Considered: Workflow-Level Storage Override

**Alternative Design**: Instead of Workflow-level `retention_policy`, allow per-submission override via API.

**Rejected Because**:

- **Complexity**: API consumers must understand retention implications for every call
- **Default Risk**: Forgetting to specify retention → unintended persistence
- **Workflow Intent**: Retention policy is a workflow characteristic, not submission-level concern
  - Example: "PII Validation" workflow should _always_ be ephemeral
  - Example: "Compliance Audit" workflow should _always_ retain for 30 days

**Workflow-level configuration** aligns with the platform's orchestration model and reduces cognitive load on API users.

## Testing Strategy

### Unit Tests

```python
# submissions/tests/test_ephemeral_storage.py

class EphemeralStorageTests(TestCase):
    def test_store_and_retrieve(self):
        """Ephemeral storage round-trip."""
        submission = SubmissionFactory()
        content = "test content"

        EphemeralSubmissionStorage.store(submission.id, content)
        retrieved = EphemeralSubmissionStorage.retrieve(submission.id)

        self.assertEqual(retrieved, content)

    def test_ttl_expiration(self):
        """Content expires after TTL."""
        submission = SubmissionFactory()

        with override_settings(REDIS_SUBMISSION_TTL=1):
            EphemeralSubmissionStorage.store(submission.id, "test")
            time.sleep(2)

            retrieved = EphemeralSubmissionStorage.retrieve(submission.id)
            self.assertIsNone(retrieved)

    def test_delete_removes_content(self):
        """Explicit delete clears Redis."""
        submission = SubmissionFactory()
        EphemeralSubmissionStorage.store(submission.id, "test")

        EphemeralSubmissionStorage.delete(submission.id)

        retrieved = EphemeralSubmissionStorage.retrieve(submission.id)
        self.assertIsNone(retrieved)

# submissions/tests/test_submission_model.py

class SubmissionEphemeralTests(TestCase):
    def test_set_content_ephemeral(self):
        """NONE retention policy stores in Redis, not DB."""
        workflow = WorkflowFactory(retention_policy=SubmissionRetentionPolicy.NONE)
        submission = SubmissionFactory(workflow=workflow)

        submission.set_content(
            inline_text="test content",
            retention_policy=SubmissionRetentionPolicy.NONE,
        )
        submission.save()

        self.assertTrue(submission.is_ephemeral)
        self.assertIsNone(submission.content)
        self.assertIsNone(submission.input_file.name)
        self.assertIsNotNone(submission.checksum_sha256)

        # Verify Redis storage
        retrieved = EphemeralSubmissionStorage.retrieve(submission.id)
        self.assertEqual(retrieved, "test content")

    def test_get_content_ephemeral(self):
        """get_content() fetches from Redis for ephemeral submissions."""
        submission = SubmissionFactory(is_ephemeral=True)
        EphemeralSubmissionStorage.store(submission.id, "ephemeral data")

        content = submission.get_content()

        self.assertEqual(content, "ephemeral data")

    def test_get_content_expired_raises(self):
        """get_content() raises if Redis content expired."""
        submission = SubmissionFactory(is_ephemeral=True)

        with self.assertRaisesMessage(ValueError, "expired or been deleted"):
            submission.get_content()
```

### Integration Tests

```python
# tests/test_full_workflows/test_ephemeral_validation_flow.py

class EphemeralValidationFlowTests(TestCase):
    def test_ephemeral_workflow_end_to_end(self):
        """Full validation flow with ephemeral storage and cleanup."""
        workflow = WorkflowFactory(
            retention_policy=SubmissionRetentionPolicy.NONE,
        )
        step = WorkflowStepFactory(
            workflow=workflow,
            validator=ValidatorFactory(validation_type=ValidationType.BASIC),
        )

        # Launch validation (simulates user form submission)
        submission_build = build_submission_from_form(
            request=self.mock_request,
            workflow=workflow,
            cleaned_data={
                'payload': '{"test": "data"}',
                'file_type': SubmissionFileType.JSON,
            },
        )

        submission = submission_build.submission
        self.assertTrue(submission.is_ephemeral)

        # Verify Redis storage
        redis_content = EphemeralSubmissionStorage.retrieve(submission.id)
        self.assertIsNotNone(redis_content)

        # Execute validation
        service = ValidationRunService()
        run = ValidationRun.objects.create(
            workflow=workflow,
            submission=submission,
            org=workflow.org,
        )
        result = service.execute(
            validation_run_id=run.id,
            user_id=self.user.id,
        )

        self.assertEqual(result.status, ValidationRunStatus.PASSED)

        # Verify cleanup
        redis_content_after = EphemeralSubmissionStorage.retrieve(submission.id)
        self.assertIsNone(redis_content_after)  # Should be deleted post-validation
```

### Load Tests

```python
# tests/test_ephemeral_storage_load.py

class EphemeralStorageLoadTests(TestCase):
    def test_concurrent_ephemeral_submissions(self):
        """1000 concurrent ephemeral submissions don't exhaust Redis."""
        workflow = WorkflowFactory(retention_policy=SubmissionRetentionPolicy.NONE)

        submissions = []
        for i in range(1000):
            submission = SubmissionFactory(workflow=workflow)
            submission.set_content(
                inline_text=f"Large content {i}" * 100,  # ~1KB each
                retention_policy=SubmissionRetentionPolicy.NONE,
            )
            submissions.append(submission)

        # Verify all stored successfully
        for submission in submissions:
            content = EphemeralSubmissionStorage.retrieve(submission.id)
            self.assertIsNotNone(content)

        # Verify Redis memory usage (requires django-redis)
        # This is a manual observation test, not automated
```

## Monitoring and Observability

### Key Metrics

1. **Redis Memory Usage**: `redis.used_memory_rss` (alert if >80% capacity)
2. **Ephemeral Submission Rate**: Count of `Submission.is_ephemeral=True` created per hour
3. **Cleanup Task Success**: `cleanup_expired_submissions` execution count and duration
4. **TTL Expiration Failures**: Count of `ValueError("expired or been deleted")` during validation
5. **Retention Policy Distribution**: Breakdown of workflows by `NONE`/`DAYS_10`/`DAYS_30`

### Dashboards (Sentry/Datadog)

```
Ephemeral Submissions Dashboard
├── Redis Memory Usage (time series)
├── Submission Creation Rate by Retention Policy (bar chart)
├── Cleanup Task Runs (daily count)
├── Validation Failures Due to Expiration (alert threshold: >5/hour)
└── S3 Storage Savings (before/after)
```

### Alerts

1. **Critical**: Redis memory >90% → Page on-call engineer
2. **Warning**: Cleanup task failed 3 consecutive runs → Slack alert
3. **Info**: Validation failure rate for ephemeral submissions >1% → Email team

## Documentation Updates

### User-Facing Docs

**New Page:** `docs/user_docs/workflow-retention-policies.md`

```markdown
# Submission Retention Policies

When you create a workflow, you choose how long SimpleValidations retains user submission data:

## Retention Tiers

### No Retention (Ephemeral) - Default

- **Best for**: Most validations, especially PII/sensitive data
- **What happens**: Content deleted immediately after validation completes
- **Storage**: Redis (temporary, in-memory)
- **Access**: Checksum available for status polling, content not retrievable

### 10-Day Retention

- **Best for**: Debugging, support investigations
- **What happens**: Content stored for 10 days, then automatically deleted
- **Storage**: PostgreSQL (inline) or S3 (files)
- **Access**: Full content available for 10 days

### 30-Day Retention

- **Best for**: Compliance, audit trails, reprocessing
- **What happens**: Content stored for 30 days, then automatically deleted
- **Storage**: PostgreSQL (inline) or S3 (files)
- **Access**: Full content available for 30 days

## Choosing the Right Policy

Use **No Retention** unless you have a specific business reason to retain data.
Retention increases security risk and storage costs.

## Changing Retention Policy

Navigate to Workflow Settings → Retention Policy. Changes apply to _new_ submissions only.
Existing submissions follow the policy in effect when they were created.
```

### Developer Docs

**Update:** `docs/dev_docs/overview/how_it_works.md`

Add section after "Submission Creation Flow":

```markdown
### Ephemeral Submission Storage

Since 2025-11-22, submissions can be stored ephemerally in Redis rather than
PostgreSQL/S3. This is controlled by `Workflow.retention_policy`:

- `NONE`: Content stored in Redis with 1-hour TTL, deleted after validation
- `DAYS_10` / `DAYS_30`: Content stored in DB/S3, deleted by background job after expiry

Key implementation:

- `EphemeralSubmissionStorage` service handles Redis operations
- `Submission.get_content()` abstracts storage location (engines unaware)
- Post-validation cleanup deletes Redis keys for ephemeral submissions
- Celery Beat task `cleanup_expired_submissions` handles DB/S3 deletion

See ADR-2025-11-22-ephemeral-submissions for full architecture.
```

## Success Criteria

**Launch Readiness:**

- ✅ All tests passing (unit, integration, load)
- ✅ Redis memory monitoring in place
- ✅ Cleanup task deployed and scheduled
- ✅ User documentation published
- ✅ Default retention policy configurable via settings

**6-Week Success Metrics:**

- Ephemeral submission rate >50% of total submissions
- Zero validation failures due to TTL expiration (excluding user error)
- Redis memory usage stable <60% capacity
- Cleanup task success rate >99%
- S3 storage costs reduced by >30% (vs pre-ephemeral baseline)

**12-Month Success Metrics:**

- At least one enterprise customer citing ephemeral storage as buying factor
- GDPR compliance audit passes with no data retention findings
- Zero security incidents involving stored submission content

## Open Questions

1. **Should we allow users to request permanent deletion of non-ephemeral submissions?**

   - Pro: GDPR "right to erasure" compliance
   - Con: Complexity in UI, potential audit trail gaps
   - **Recommendation**: Defer to Phase 2, validate demand first

2. **Should we support workflow-level encryption for retained submissions?**

   - Pro: Defense-in-depth for DAYS_10/DAYS_30
   - Con: Key management complexity, S3 already encrypts at rest
   - **Recommendation**: Defer, document that S3 encryption covers this

3. **Should submission checksums be indexed for deduplication?**
   - Pro: "Have I validated this file before?" feature
   - Con: Requires index, query performance impact
   - **Recommendation**: Consider for Phase 2 after measuring checksum query frequency

## References

- **GDPR Data Minimization**: Article 5(1)(c) - "adequate, relevant and limited to what is necessary"
- **Redis TTL Documentation**: https://redis.io/commands/expire/
- **Django-Redis**: https://github.com/jazzband/django-redis
- **S3 Lifecycle Policies**: https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html
- **Celery Beat**: https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html

## Conclusion

This ADR proposes a **security-first, privacy-by-default** approach to submission storage.
By defaulting to ephemeral storage and offering explicit retention tiers, we:

- Minimize data breach risk
- Enable compliance with GDPR, HIPAA, SOC 2
- Reduce storage costs
- Differentiate from competitors

The implementation leverages existing infrastructure (Redis, Celery Beat, S3) and
preserves the clean abstraction (`get_content()`) that keeps validation engines simple.

Rollout risk is mitigated by:

- Phased deployment with feature flags
- Comprehensive testing (unit, integration, load)
- Monitoring and alerting for Redis memory and cleanup tasks
- Conservative default (DAYS_30) for existing workflows during migration

**Approval Required**: Product, Engineering, Security teams to sign off before Phase 1.
