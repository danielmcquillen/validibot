# Submissions

A **Submission** is the entry point into the system.  
It represents:

- The file being validated (JSON, XML, EnergyPlus IDF, etc.).
- The workflow _version_ to run.
- The organization, project, and user context.
- Metadata such as content type, size, and SHA-256 checksum.

Submissions can have multiple **Validation Runs** over time, but typically point to the _latest run_.

## User Context

`Submission.user` captures the human (or service) that supplied the payload. We
store it even though each `ValidationRun` also has a `user` field because a
single submission can be re-run many times by different operators:

- A data engineer uploads a file, but an admin later replays the same submission
  to verify fixes.
- An API integration pushes content using an org-level API token where no Django
  `User` instance exists.
- Background processes can enqueue submissions on behalf of a workflow (for
  example, nightly batch imports) without an authenticated user object.

Those flows mean `Submission.user` is nullable. When it is `NULL` we rely on the
organization/project ForeignKeys and metadata provided in the payload (API key,
signed request, etc.) to decide who owns the submission.

## Relationship to Validation Runs

A `ValidationRun` references the submission that triggered it, but it records the
user who _executed the run_. Keeping both fields lets the audit trail answer two
questions:

1. **Who provided the content?** → `submission.user`
2. **Who triggered this execution?** → `validation_run.user`

When you launch a run via the UI, both values usually match. When executions are
scheduled, retried by Celery, or invoked via an API key, the run user may be
`NULL` or different from the submission user. Treat the submission record as the
ownership anchor for the payload itself, and the run record as the executor
context for a single processing attempt.

## Data Retention

Submissions support configurable retention policies that control how long the
actual content (file or inline text) is stored. This supports compliance requirements
and reduces storage costs for workflows that don't need to retain user data.

### Retention Policies

| Policy | Behavior |
|--------|----------|
| `DO_NOT_STORE` | Content deleted after validation completes |
| `STORE_1_DAY` | Content retained for 1 day |
| `STORE_7_DAYS` | Content retained for 7 days |
| `STORE_30_DAYS` | Content retained for 30 days |
| `STORE_PERMANENTLY` | Content retained indefinitely |

### Key Fields

- `retention_policy`: Snapshot of the workflow's retention setting at submission time
- `expires_at`: When content should be purged (null for DO_NOT_STORE or already purged)
- `content_purged_at`: Timestamp when content was purged (audit trail)

### Content Purge vs Record Deletion

When a submission's retention expires, we **purge the content** but **preserve the record**.
This means:

- The `Submission` row remains in the database with its metadata intact
- `content` is cleared to empty string
- `input_file` is deleted from storage
- `checksum_sha256`, `original_filename`, `size_bytes` are preserved for audit
- Associated GCS execution bundles (`gs://bucket/runs/{org}/{run}/`) are deleted

This approach preserves the audit trail while removing the actual user data.

### Defensive FK: ValidationRun.submission

`ValidationRun.submission` uses `SET_NULL` instead of `CASCADE`. This means:

- If a Submission record is accidentally deleted, the ValidationRun survives
- Code accessing `run.submission` must handle `None`
- API responses show `"submission": null` when unavailable

### Management Commands

Two commands handle retention:

```bash
# Purge submissions past their expires_at date (run hourly)
python manage.py purge_expired_submissions --batch-size 100

# Process failed purge attempts (run every 5 minutes)
python manage.py process_purge_retries --batch-size 50
```

### PurgeRetry Model

When a purge fails (e.g., GCS unavailable), a `PurgeRetry` record is created
for automatic retry with exponential backoff. After 5 failed attempts, manual
intervention is required.
