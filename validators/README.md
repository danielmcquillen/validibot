# Validators

This directory contains Cloud Run Job validator containers. Each validator runs as an independent Cloud Run Job triggered by the Django app (running as a Cloud Run Service).

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│ Cloud Run Service (Django)                                          │
│                                                                      │
│ 1. Creates ValidationInputEnvelope                                  │
│ 2. Uploads to GCS as input.json                                     │
│ 3. Triggers Cloud Run Job via Cloud Tasks                           │
│ 4. Receives callback when complete                                  │
│ 5. Downloads output.json from GCS                                   │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Cloud Run Job (Validator Container)                                 │
│                                                                      │
│ 1. Downloads input.json from GCS                                    │
│ 2. Deserializes to typed envelope (e.g., EnergyPlusInputEnvelope)  │
│ 3. Runs validation/simulation                                       │
│ 4. Creates typed output envelope (e.g., EnergyPlusOutputEnvelope)  │
│ 5. Uploads output.json to GCS                                       │
│ 6. POSTs minimal callback to Django                                 │
└─────────────────────────────────────────────────────────────────────┘
```

## Directory Structure

```
validators/
├── shared/                   # Shared utilities for all validators
│   ├── gcs_client.py        # GCS download/upload helpers
│   ├── callback_client.py   # HTTP callback utilities
│   └── envelope_loader.py   # Envelope serialization helpers
│
├── energyplus/              # EnergyPlus validator container
│   ├── Dockerfile
│   ├── main.py              # Entrypoint for Cloud Run Job
│   ├── runner.py            # EnergyPlus execution logic
│   ├── requirements.txt
│   ├── README.md
│   └── tests/
│
└── (future validators: fmi/, xml/, pdf/, etc.)
```

## Validator Container Contract

Each validator container MUST:

1. **Accept environment variables**:
   - `INPUT_URI`: GCS URI to input.json (e.g., `gs://bucket/org_id/run_id/input.json`)
   - Or accept input.json path as first command-line argument

2. **Download input envelope from GCS**:
   ```python
   from validators.shared.gcs_client import download_envelope
   from sv_shared.energyplus.envelopes import EnergyPlusInputEnvelope

   input_envelope = download_envelope(INPUT_URI, EnergyPlusInputEnvelope)
   ```

3. **Run validation/simulation**:
   - Use typed configuration from `input_envelope.inputs`
   - Download input files from GCS URIs in `input_envelope.input_files`
   - Execute domain-specific logic (EnergyPlus sim, FMU probe, XML validation, etc.)

4. **Create output envelope**:
   ```python
   from sv_shared.energyplus.envelopes import EnergyPlusOutputEnvelope
   from sv_shared.validations.envelopes import ValidationStatus

   output_envelope = EnergyPlusOutputEnvelope(
       run_id=input_envelope.run_id,
       validator=input_envelope.validator,
       status=ValidationStatus.SUCCESS,
       timing=...,
       messages=[...],
       metrics=[...],
       outputs=EnergyPlusOutputs(...)
   )
   ```

5. **Upload output envelope to GCS**:
   ```python
   from validators.shared.gcs_client import upload_envelope

   output_uri = f"{input_envelope.context.execution_bundle_uri}/output.json"
   upload_envelope(output_envelope, output_uri)
   ```

6. **POST callback to Django**:
   ```python
   from validators.shared.callback_client import post_callback

   post_callback(
       callback_url=input_envelope.context.callback_url,
       callback_token=input_envelope.context.callback_token,
       run_id=input_envelope.run_id,
       status=ValidationStatus.SUCCESS,
       result_uri=output_uri
   )
   ```

## Dependencies

All validators depend on `sv_shared`:

```toml
# In each validator's requirements.txt or pyproject.toml
sv-shared @ git+https://github.com/YOUR_ORG/validibot.git@main#subdirectory=sv_shared
```

This ensures validators and Django use identical envelope schemas.

## Deployment

Each validator is deployed as a separate Cloud Run Job:

```bash
# Build and push container
gcloud builds submit --tag gcr.io/PROJECT/validibot-validator-energyplus validators/energyplus

# Create or update Cloud Run Job
gcloud run jobs create validibot-validator-energyplus \
  --image gcr.io/PROJECT/validibot-validator-energyplus \
  --region us-central1 \
  --memory 4Gi \
  --cpu 2 \
  --max-retries 0 \
  --task-timeout 3600
```

## Testing

Each validator should include:

1. **Unit tests**: Test runner logic in isolation
2. **Integration tests**: Test full envelope → execution → callback flow
3. **Example inputs**: Sample input.json files for manual testing

Run tests:
```bash
cd validators/energyplus
pytest tests/
```

## Adding a New Validator

1. Copy `validators/energyplus/` as a template
2. Update `Dockerfile` with domain-specific dependencies
3. Implement `runner.py` with validation logic
4. Create typed envelope subclasses in `sv_shared/{domain}/envelopes.py`
5. Update `main.py` to use your typed envelopes
6. Write tests
7. Deploy as new Cloud Run Job
