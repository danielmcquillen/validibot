# ADR-2026-01-08: Auxiliary Files for Workflow Submissions

**Status:** Proposed
**Owners:** Platform / Workflows
**Related ADRs:** None
**Related docs:** `workflows/models.py`, `submissions/models.py`, `validations/services/cloud_run/launcher.py`

---

## Context

### The Problem

Some validators require additional files beyond the primary submission. For example:

- **EnergyPlus** requires a weather file (EPW) alongside the building model (IDF/epJSON)
- **FMI validators** may require input CSV files with time-series data
- **XML validators** may need external DTD or schema references

Currently, we handle the EnergyPlus weather file by having workflow authors select a pre-configured weather file from a dropdown. This works for fixed-location compliance workflows (e.g., "San Francisco building code") but doesn't support:

1. Submitters who need to provide their own location-specific weather data
2. Validators that require truly submission-specific auxiliary files
3. Future validators we haven't anticipated yet

### Design Goals

1. **EnergyPlus first** — Design around the weather file use case, but...
2. **Extensible** — Support arbitrary auxiliary file types for future validators
3. **Safe** — Constrain file types and sizes to prevent abuse
4. **Author-guided** — Workflow authors define what auxiliary files are needed
5. **Validator-aware** — Warn authors when a validator requires auxiliary files they haven't configured

---

## Decision

### 1. Workflow-Level Auxiliary File Definitions

Workflow authors can define auxiliary file "slots" that submissions must fill. Each slot specifies:

```python
class WorkflowAuxiliaryFileSlot(models.Model):
    """
    Defines an auxiliary file slot for a workflow.

    Example: An EnergyPlus workflow might have a slot for "weather_file"
    with file_types=["EPW"] and required=True.
    """
    workflow = models.ForeignKey(Workflow, related_name="auxiliary_file_slots", ...)

    # Identity
    slug = models.SlugField(max_length=50)  # e.g., "weather_file", "input_data"
    label = models.CharField(max_length=100)  # e.g., "Weather File", "Input Data CSV"
    description = models.TextField(blank=True)  # Help text for submitters

    # Constraints
    allowed_file_types = ArrayField(
        models.CharField(max_length=20),
        help_text="Allowed extensions: epw, csv, json, xml",
    )
    max_size_bytes = models.PositiveIntegerField(default=10 * 1024 * 1024)  # 10MB default
    required = models.BooleanField(default=True)

    # Ordering
    order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("workflow", "slug")]
        ordering = ["order"]
```

### 2. Allowed File Types (Phase 1)

To limit attack surface, we start with a conservative allowlist:

| Extension | MIME Type | Use Case |
|-----------|-----------|----------|
| `epw` | `application/vnd.energyplus.epw` | EnergyPlus weather data |
| `csv` | `text/csv` | Time-series input data |
| `json` | `application/json` | Configuration, parameters |
| `xml` | `application/xml` | Schema references, configs |

Additional types can be added via settings:

```python
# settings.py
AUXILIARY_FILE_ALLOWED_TYPES = {
    "epw": {
        "mime_type": "application/vnd.energyplus.epw",
        "max_size_mb": 20,
        "description": "EnergyPlus Weather File",
    },
    "csv": {
        "mime_type": "text/csv",
        "max_size_mb": 10,
        "description": "Comma-separated values",
    },
    # ...
}
```

### 3. Submission-Level Auxiliary Files

When a submission is created, auxiliary files are stored alongside it:

```python
class SubmissionAuxiliaryFile(models.Model):
    """
    An auxiliary file attached to a submission.

    Links to a WorkflowAuxiliaryFileSlot to identify which slot this fills.
    """
    submission = models.ForeignKey(Submission, related_name="auxiliary_files", ...)
    slot = models.ForeignKey(WorkflowAuxiliaryFileSlot, ...)

    # Storage (same pattern as Submission.file)
    file = models.FileField(upload_to=auxiliary_file_path, storage=...)
    original_filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=20)  # e.g., "epw"
    size_bytes = models.PositiveIntegerField()

    # GCS URI (populated after upload to private bucket)
    gcs_uri = models.CharField(max_length=500, blank=True)

    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("submission", "slot")]  # One file per slot per submission
```

### 4. Limits (Phase 1)

To prevent abuse:

- **Maximum slots per workflow:** 3
- **Maximum file size per slot:** Configurable, default 10MB
- **Maximum total auxiliary size per submission:** 50MB
- **File type allowlist:** Enforced at upload time

### 5. Step Configuration Integration

The weather file dropdown becomes a special case of auxiliary file configuration:

```python
# WorkflowStep.config for EnergyPlus
{
    "weather_file_source": "workflow",  # or "submitter"
    "weather_file": "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",  # If "workflow"
    "weather_file_slot": "weather_file",  # If "submitter" - references slot.slug
    ...
}
```

When `weather_file_source` is `"submitter"`:
1. The workflow must have an auxiliary file slot with `slug="weather_file"`
2. The launcher reads the file URI from the submission's auxiliary files
3. The file is passed to the Cloud Run Job via the input envelope

### 6. Author-Time Validation

When saving a workflow step:

```python
def validate_step_auxiliary_requirements(step: WorkflowStep) -> list[str]:
    """
    Check that the workflow has required auxiliary file slots for the validator.
    Returns list of warning messages.
    """
    warnings = []
    validator = step.validator

    if validator.validation_type == ValidationType.ENERGYPLUS:
        config = step.config or {}
        if config.get("weather_file_source") == "submitter":
            # Check workflow has weather_file slot
            slot = step.workflow.auxiliary_file_slots.filter(slug="weather_file").first()
            if not slot:
                warnings.append(
                    "This step requires submitters to provide a weather file, "
                    "but the workflow has no 'weather_file' auxiliary file slot configured."
                )
            elif "epw" not in slot.allowed_file_types:
                warnings.append(
                    "The 'weather_file' slot does not allow EPW files."
                )

    return warnings
```

### 7. Launch Form Integration

The workflow launch form dynamically shows auxiliary file uploads:

```python
class WorkflowLaunchForm(forms.Form):
    # ... existing fields ...

    def __init__(self, *args, workflow=None, **kwargs):
        super().__init__(*args, **kwargs)

        # Add a FileField for each required auxiliary file slot
        for slot in workflow.auxiliary_file_slots.all():
            field_name = f"aux_{slot.slug}"
            self.fields[field_name] = forms.FileField(
                label=slot.label,
                help_text=slot.description,
                required=slot.required,
            )
```

In the template, the "Extra Data" tab shows these fields when present:

```html
{% if form.has_auxiliary_file_fields %}
<div class="auxiliary-files-section">
    <h6>Additional Files</h6>
    {% for slot in workflow.auxiliary_file_slots.all %}
        {{ form|get_field:slot.field_name }}
    {% endfor %}
</div>
{% endif %}
```

### 8. Launcher Integration

The launcher reads auxiliary files from the submission and includes them in the envelope:

```python
# launcher.py - launch_energyplus_validation()

# Determine weather file source
step_config = step.config or {}
weather_source = step_config.get("weather_file_source", "workflow")

if weather_source == "workflow":
    # Existing behavior: use pre-configured weather file
    weather_file = step_config.get("weather_file")
    weather_file_uri = f"gs://{bucket}/{weather_prefix}/{weather_file}"
else:
    # New: get from submission auxiliary files
    aux_file = submission.auxiliary_files.filter(slot__slug="weather_file").first()
    if not aux_file:
        raise ValueError("Submission missing required weather file")
    weather_file_uri = aux_file.gcs_uri
```

---

## Flow Diagram

```
┌────────────────────────────────────────────────────────────────────┐
│                        Workflow Configuration                       │
└────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│ Author defines auxiliary file slots:                                │
│   - slug: "weather_file"                                           │
│   - label: "Weather File (EPW)"                                    │
│   - allowed_file_types: ["epw"]                                    │
│   - required: true                                                 │
│   - max_size_bytes: 20MB                                           │
└────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│ Author configures EnergyPlus step:                                  │
│   - weather_file_source: "submitter"                               │
│   - weather_file_slot: "weather_file"                              │
└────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│                         Submission Time                             │
└────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│ Launch form shows:                                                  │
│   - Primary file upload (IDF/epJSON)                               │
│   - "Additional Files" section:                                    │
│       └─ Weather File (EPW) [required]                             │
└────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│ On submit:                                                          │
│   1. Validate primary file                                         │
│   2. Validate auxiliary files (type, size)                         │
│   3. Upload all files to GCS                                       │
│   4. Create Submission + SubmissionAuxiliaryFile records           │
│   5. Start validation run                                          │
└────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│ Launcher:                                                           │
│   1. Read weather_file_source from step config                     │
│   2. If "submitter": get GCS URI from SubmissionAuxiliaryFile      │
│   3. Build envelope with weather_file_uri                          │
│   4. Trigger Cloud Run Job                                         │
└────────────────────────────────────────────────────────────────────┘
```

---

## Security Considerations

### File Type Validation

1. **Extension check** — Verify file extension matches allowed types
2. **MIME type check** — Validate Content-Type header matches expected
3. **Magic bytes check** — For known formats (EPW, CSV), validate file headers
4. **Size limits** — Enforce per-slot and per-submission limits

### Storage Security

1. **Private bucket** — Auxiliary files stored in `validibot-files` (private), not media bucket
2. **Signed URLs** — If files need to be downloadable, use time-limited signed URLs
3. **Org isolation** — File paths include org ID: `submissions/{org_id}/{submission_id}/aux/{slot_slug}/{filename}`

### Input Sanitization

For text-based formats (CSV, JSON, XML):

1. **Encoding validation** — Ensure valid UTF-8
2. **Size limits** — Enforce before parsing
3. **No path traversal** — Sanitize filenames to prevent `../` attacks

---

## Consequences

### Positive

1. **Flexible** — Supports location-specific EnergyPlus workflows
2. **Extensible** — Same mechanism works for future validators
3. **Author-controlled** — Workflow authors decide what's needed
4. **Type-safe** — Constrained to known file types

### Negative

1. **UI complexity** — Launch form becomes more complex
2. **Storage costs** — More files to store and manage
3. **Migration needed** — Existing EnergyPlus workflows need migration path

### Neutral

1. **Backwards compatible** — Existing "pre-configured weather file" option remains
2. **Optional feature** — Workflows without auxiliary slots work as before

---

## Implementation Phases

### Phase 1: Data Model + EnergyPlus Weather Files

1. [ ] Create `WorkflowAuxiliaryFileSlot` model
2. [ ] Create `SubmissionAuxiliaryFile` model
3. [ ] Add migrations
4. [ ] Update EnergyPlus step config form with "Provided by submitter" option
5. [ ] Update `WorkflowLaunchForm` to show auxiliary file fields
6. [ ] Update launcher to read from auxiliary files
7. [ ] Write tests

### Phase 2: Author-Time Validation

1. [ ] Add step validation for required auxiliary slots
2. [ ] Add warning UI in step editor
3. [ ] Add workflow validation summary

### Phase 3: Additional File Types

1. [ ] Add CSV support for FMI validators
2. [ ] Evaluate additional types based on user feedback

---

## API Changes

### Launch API

The `/api/v1/org/{org}/workflows/{id}/runs/` endpoint would accept multipart form data:

```
POST /api/v1/org/acme/workflows/abc123/runs/
Content-Type: multipart/form-data

--boundary
Content-Disposition: form-data; name="payload"
Content-Type: application/json

{"Building": {...}}
--boundary
Content-Disposition: form-data; name="aux_weather_file"; filename="chicago.epw"
Content-Type: application/vnd.energyplus.epw

[EPW file contents]
--boundary--
```

### CLI Changes

The CLI would support auxiliary files:

```bash
validibot run workflow-abc \
    --file model.idf \
    --aux weather_file=chicago.epw
```

---

## Alternatives Considered

### 1. Inline Auxiliary Data in Submission Content

Store auxiliary file contents as base64 in the submission JSON.

**Rejected because:**
- Bloats submission size (base64 is ~33% larger)
- Complicates submission model
- Doesn't work well for binary files

### 2. Separate "Attachments" Endpoint

Upload auxiliary files separately, then reference by ID in submission.

**Rejected because:**
- Adds complexity (multiple upload steps)
- Orphaned attachments if submission fails
- Harder to understand flow

### 3. Generic "File Collection" Model

A more abstract model where any entity can have attached files.

**Deferred because:**
- Over-engineered for current needs
- Can migrate to this later if needed

---

## References

- EnergyPlus Weather Files: https://energyplus.net/weather
- Current weather file implementation: `validibot/workflows/forms.py` (`EnergyPlusStepConfigForm`)
- Cloud Run Job launcher: `validibot/validations/services/cloud_run/launcher.py`
