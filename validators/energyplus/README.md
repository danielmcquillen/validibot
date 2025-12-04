# EnergyPlus Validator Container

Cloud Run Job container for running EnergyPlus simulations as part of validation workflows.

## Overview

This container:
1. Downloads `input.json` (EnergyPlusInputEnvelope) from GCS
2. Downloads IDF/epJSON model and EPW weather files from GCS
3. Runs EnergyPlus simulation
4. Extracts metrics from SQL database
5. Creates `output.json` (EnergyPlusOutputEnvelope) with results
6. Uploads output.json to GCS
7. POSTs callback to Django

## Container Interface

### Environment Variables

- `INPUT_URI` (required): GCS URI to input.json (e.g., `gs://bucket/org_id/run_id/input.json`)
- `GOOGLE_CLOUD_PROJECT`: GCP project ID (auto-set by Cloud Run)

### Input Envelope Structure

```json
{
  "schema_version": "validibot.input.v1",
  "run_id": "abc-123",
  "validator": {
    "id": "validator-uuid",
    "type": "energyplus",
    "version": "24.2.0"
  },
  "input_files": [
    {
      "name": "model.idf",
      "mime_type": "application/vnd.energyplus.idf",
      "role": "primary-model",
      "uri": "gs://bucket/models/model.idf"
    },
    {
      "name": "weather.epw",
      "mime_type": "application/vnd.energyplus.epw",
      "role": "weather",
      "uri": "gs://bucket/weather/USA_CA_SF.epw"
    }
  ],
  "inputs": {
    "timestep_per_hour": 4,
    "output_variables": ["Zone Mean Air Temperature"],
    "invocation_mode": "cli"
  },
  "context": {
    "callback_url": "https://validibot.example.com/api/v1/validation-callbacks/",
    "callback_token": "eyJ...",
    "execution_bundle_uri": "gs://bucket/org_id/run_id/",
    "timeout_seconds": 3600
  }
}
```

### Output Envelope Structure

```json
{
  "schema_version": "validibot.output.v1",
  "run_id": "abc-123",
  "validator": {
    "id": "validator-uuid",
    "type": "energyplus",
    "version": "24.2.0"
  },
  "status": "success",
  "timing": {
    "started_at": "2025-12-04T10:00:00Z",
    "finished_at": "2025-12-04T10:05:30Z"
  },
  "messages": [
    {
      "severity": "INFO",
      "text": "Simulation completed successfully"
    }
  ],
  "metrics": [
    {
      "name": "electricity_kwh",
      "value": 1234.5,
      "unit": "kWh",
      "category": "energy"
    },
    {
      "name": "energy_use_intensity_kwh_m2",
      "value": 18.7,
      "unit": "kWh/m²",
      "category": "energy"
    }
  ],
  "artifacts": [
    {
      "name": "simulation.sql",
      "type": "simulation-db",
      "mime_type": "application/x-sqlite3",
      "uri": "gs://bucket/org_id/run_id/outputs/eplusout.sql",
      "size_bytes": 524288
    }
  ],
  "outputs": {
    "outputs": {
      "eplusout_sql": "/tmp/run/eplusout.sql",
      "eplusout_err": "/tmp/run/eplusout.err"
    },
    "metrics": {
      "electricity_kwh": 1234.5,
      "energy_use_intensity_kwh_m2": 18.7
    },
    "logs": {
      "stdout_tail": "...",
      "stderr_tail": "...",
      "err_tail": "..."
    },
    "energyplus_returncode": 0,
    "execution_seconds": 330.5,
    "invocation_mode": "cli"
  }
}
```

## Building and Deploying

### Build Container

```bash
cd validators/energyplus
docker build -t gcr.io/PROJECT_ID/validibot-validator-energyplus:latest .
docker push gcr.io/PROJECT_ID/validibot-validator-energyplus:latest
```

### Deploy Cloud Run Job

```bash
gcloud run jobs create validibot-validator-energyplus \
  --image gcr.io/PROJECT_ID/validibot-validator-energyplus:latest \
  --region us-central1 \
  --memory 4Gi \
  --cpu 2 \
  --max-retries 0 \
  --task-timeout 3600 \
  --service-account validator-runner@PROJECT_ID.iam.gserviceaccount.com
```

### Execute Job (for testing)

```bash
gcloud run jobs execute validibot-validator-energyplus \
  --region us-central1 \
  --env-vars INPUT_URI=gs://bucket/test/input.json
```

## Local Development

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Run Locally

```bash
# Set input URI
export INPUT_URI=gs://your-bucket/test-run/input.json

# Run validator
python main.py
```

### Run Tests

```bash
pytest tests/
```

## EnergyPlus Version

This container uses EnergyPlus 24.2.0. To update:

1. Modify `Dockerfile` to install different version
2. Update `validator.version` in Django database
3. Rebuild and redeploy container
