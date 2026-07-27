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
- `expires_at`: Provisional receipt-time deadline, reset after the last related
  run to give the full author-selected post-processing window (null for
  `DO_NOT_STORE`, permanent retention, or already purged)
- `content_purged_at`: Timestamp when content was purged (audit trail)

### Content Purge vs Record Deletion

When a submission's retention expires, we **purge the content** but **preserve
a minimal record**.
This means:

- The `Submission` row remains in the database
- `content` is cleared to empty string
- `input_file` is deleted from storage
- submitter-supplied names, original filenames, and arbitrary metadata are cleared
- `checksum_sha256`, `size_bytes`, `file_type`, and timestamps remain for audit
- auxiliary `SubmissionInputFile` bytes and submitter context are removed
- copied run inputs and input envelopes are deleted, while independently
  retained outputs remain until their own policy expires

No purge runs while a related validation is active. Shared inputs are eligible
only after every related run is terminal.

### Defensive FK: ValidationRun.submission

`ValidationRun.submission` uses `SET_NULL` instead of `CASCADE`. This means:

- If a Submission record is accidentally deleted, the ValidationRun survives
- Code accessing `run.submission` must handle `None`
- API responses show `"submission": null` when unavailable

### Management Commands

Two commands handle retention:

```bash
# Purge finite-retention submissions past expires_at (run hourly)
python manage.py purge_expired_submissions --batch-size 100

# Process no-retention and failed purge work (run every 5 minutes)
python manage.py process_purge_retries --batch-size 50
```

### PurgeRetry Model

One `PurgeRetry` row exists per submission. Failed deletion uses capped
exponential backoff (1 minute, 5 minutes, 1 hour, 6 hours, then 24 hours).
Crossing five attempts raises an operator warning but never abandons
privacy-critical deletion. A repair scan recreates missing no-retention work
after missed hooks.
