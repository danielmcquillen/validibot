# Validation Results

When a validation run completes, three types of output are produced:

- **Findings** -- individual issues, warnings, and info messages
- **Artifacts** -- files produced by validators (reports, logs, transformed data)
- **Summaries** -- aggregated counts for dashboards and long-term reporting

These all hang off the `ValidationRun` and `ValidationStepRun` models:

```
ValidationRun
├── findings (many ValidationFinding rows)
├── artifacts (many Artifact files)
├── step_runs (many ValidationStepRun)
│   └── findings (many ValidationFinding rows)
└── summary_record (one ValidationRunSummary)
    └── step_summaries (many ValidationStepRunSummary)
```

---

## Findings

A `ValidationFinding` is a single normalized issue discovered during validation. This is the primary unit of feedback that users see in the results table.

### Fields

| Field | Type | Purpose |
|-------|------|---------|
| `validation_run` | FK(ValidationRun) | Parent run (denormalized for query performance) |
| `validation_step_run` | FK(ValidationStepRun) | The step execution that produced this finding |
| `ruleset_assertion` | FK(RulesetAssertion, nullable) | The assertion rule that triggered it, if applicable |
| `severity` | CharField | `SUCCESS`, `INFO`, `WARNING`, or `ERROR` |
| `code` | CharField | Machine-readable code (e.g. `json.schema.required`) |
| `message` | TextField | Human-readable description shown to the user |
| `path` | CharField | Location in the input data (JSON Pointer, XPath, or dotted path) |
| `meta` | JSONField | Optional extra metadata (line number, variable name, etc.) |

Findings are ordered by severity (ERROR first, then WARNING, INFO, SUCCESS) and then by creation date descending.

### How findings are created

Validators don't write to the database directly. Instead, they emit `ValidationIssue` dataclass objects during execution. After a step finishes, the `FindingsPersistence` service normalizes these issues and bulk-creates `ValidationFinding` rows.

The flow:

```
Validator engine
  → emits ValidationIssue objects
    → collected in ValidationResult.issues
      → FindingsPersistence.persist_findings()
        → bulk-creates ValidationFinding rows
```

For advanced (async) validators like EnergyPlus, findings are produced in two stages. Input-stage findings (from assertions evaluated before the container runs) are persisted immediately. Output-stage findings (from the container's results and post-execution assertions) are appended when the callback arrives.

### Severity levels

| Severity | Meaning | Effect on run status |
|----------|---------|---------------------|
| `ERROR` | Blocking issue | Fails the step and the run |
| `WARNING` | Non-blocking issue that deserves review | Does not block |
| `INFO` | Informational message from the validator | Does not block |
| `SUCCESS` | Assertion passed (positive feedback) | Does not block |

SUCCESS findings are only created when an assertion passes **and** either the assertion has a custom `success_message` or the step has `show_success_messages=True`. See the [assertions documentation](assertions.md#success-messages) for details.

### Path field

The `path` field tells the user where in their input data the issue was found. The format depends on the validator:

- JSON Schema validators produce JSON Pointers (e.g. `/building/zones/0/name`)
- XML Schema validators produce XPaths
- CEL assertions produce dotted paths

For JSON submissions, a synthetic `payload` prefix is automatically stripped before storage so users see clean paths.

---

## Artifacts

An `Artifact` is a file produced during a validation run. Common examples include EnergyPlus simulation output, transformed data files, and debug logs.

### Fields

| Field | Type | Purpose |
|-------|------|---------|
| `org` | FK(Organization) | Owning organization |
| `validation_run` | FK(ValidationRun) | The run that produced this artifact |
| `label` | CharField | Human-readable name (e.g. "EnergyPlus Output") |
| `content_type` | CharField | MIME type (e.g. `application/json`) |
| `file` | FileField | The stored file |
| `size_bytes` | BigIntegerField | File size for display and quota tracking |

Artifact files are stored at `artifacts/org-{org_id}/runs/{run_id}/{uuid}/{filename}`.

Not all validators produce artifacts. Built-in validators (JSON Schema, XML Schema, CEL) typically produce only findings. Advanced validators running in containers (EnergyPlus, FMI) produce artifacts alongside their findings.

---

## Summaries

Summaries are lightweight aggregate snapshots that persist even after findings and artifacts are purged by the retention policy. They power dashboards and long-term reporting.

### ValidationRunSummary

One record per run with rolled-up counts:

| Field | Type | Purpose |
|-------|------|---------|
| `run` | OneToOne(ValidationRun) | The run being summarized |
| `status` | CharField | Final run status |
| `completed_at` | DateTimeField | When the run finished |
| `total_findings` | PositiveIntegerField | Total findings across all severities |
| `error_count` | PositiveIntegerField | ERROR findings |
| `warning_count` | PositiveIntegerField | WARNING findings |
| `info_count` | PositiveIntegerField | INFO findings |
| `assertion_total_count` | PositiveIntegerField | Total assertions evaluated |
| `assertion_failure_count` | PositiveIntegerField | Assertions that failed (ERROR severity only) |
| `extras` | JSONField | Optional metadata (e.g. exemplar messages for reporting) |

### ValidationStepRunSummary

One record per step within a run summary:

| Field | Type | Purpose |
|-------|------|---------|
| `summary` | FK(ValidationRunSummary) | Parent summary |
| `step_run` | OneToOne(ValidationStepRun, nullable) | Can be NULL after cleanup |
| `step_name` | CharField | Denormalized step name |
| `step_order` | PositiveIntegerField | Denormalized step position |
| `status` | CharField | Step status (PASSED, FAILED, SKIPPED, etc.) |
| `error_count` | PositiveIntegerField | ERROR findings for this step |
| `warning_count` | PositiveIntegerField | WARNING findings for this step |
| `info_count` | PositiveIntegerField | INFO findings for this step |

Step names and order are denormalized into the summary so the data remains meaningful even if the workflow definition changes later.

---

## Retention and cleanup

Each workflow defines two retention policies that control how long results are kept:

- **`data_retention`** -- how long to keep user-submitted files. Default is `DO_NOT_STORE` (files are deleted immediately after validation completes; the submission record is preserved).
- **`output_retention`** -- how long to keep validation outputs (findings, artifacts). Default is 30 days. Options range from 7 days to permanent.

When outputs expire, the `purge_expired_outputs` management command deletes findings and artifact files. The summary records are deliberately preserved, so dashboards and historical pass-rate charts continue to work even after the detailed results are gone.
