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
