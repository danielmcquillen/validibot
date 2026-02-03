# Running Validations

Once you have an active workflow, you can run validations against your data. This guide covers the different ways to launch validations and how to monitor their progress.

## Launching from the Dashboard

The quickest way to run a validation is from the Validibot UI:

1. Navigate to **Workflows** and find the workflow you want to run.
2. Click **Launch** (this button is only available for active workflows).
3. In the launch dialog:
   - **Select file type**: If the workflow accepts multiple formats, choose the one matching your data.
   - **Upload or paste content**: Either drag a file to the upload area or paste content directly.
   - **Add metadata** (optional): Attach key-value pairs to track the source or context of this submission.
4. Click **Run** to start the validation.

You'll be redirected to the run detail page where you can watch the validation progress.

## Launching with File Uploads

When uploading files through the UI:

**Supported formats**: The available formats depend on what the workflow allows. Common types include:

- **JSON** (`.json`) — Structured data, configuration files
- **XML** (`.xml`) — Markup documents, building models
- **TEXT** (`.txt`, `.idf`) — Plain text, EnergyPlus IDF files
- **YAML** (`.yaml`, `.yml`) — Configuration files
- **CSV** (`.csv`) — Tabular data

**Size limits**: The default maximum file size is configured by your administrator. For very large files, consider using the API with streaming uploads.

**File naming**: Validibot captures the original filename and stores it with the submission. This helps identify runs later.

## Launching Through the API

For automation and CI/CD integration, use the REST API to submit validations programmatically.

### Basic API Request

```bash
curl -X POST \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @payload.json \
  "https://your-validibot.com/api/v1/orgs/my-org/workflows/my-workflow/runs/"
```

The `workflow_identifier` in the URL can be either the workflow's slug (recommended) or its numeric ID.

### Request Modes

The API supports three ways to submit data:

**Raw Body Mode**: Send the file content directly as the request body. Set the `Content-Type` header to match your data format.

```bash
curl -X POST \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Filename: config.json" \
  --data-binary @config.json \
  "https://your-validibot.com/api/v1/orgs/my-org/workflows/schema-check/runs/"
```

**JSON Envelope Mode**: Wrap your content in a JSON object with metadata. Useful when you need to include additional context.

```bash
curl -X POST \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "content": "<root><value>1</value></root>",
        "content_type": "application/xml",
        "filename": "data.xml",
        "metadata": {"source": "api", "batch_id": "2024-Q1"}
      }' \
  "https://your-validibot.com/api/v1/orgs/my-org/workflows/xml-check/runs/"
```

**Multipart Mode**: Use form-based uploads for browser integrations or when working with binary files.

```bash
curl -X POST \
  -H "Authorization: Bearer $API_TOKEN" \
  -F "file=@building.idf" \
  -F "filename=building.idf" \
  -F 'metadata={"source": "upload-form"}' \
  "https://your-validibot.com/api/v1/orgs/my-org/workflows/idf-check/runs/"
```

For complete API details, see [Sending Data to the API](api/sending-data.md).

### API Permissions

To run validations via API, you need:

- A valid API token (see [Authentication](api/authentication.md))
- **Executor** role (or higher) in the target organization
- The workflow must be active

## Monitoring Progress

### Run Status

Each validation run has a status that shows where it is in the lifecycle:

| Status | Meaning |
|--------|---------|
| **PENDING** | Run created, waiting to start |
| **RUNNING** | Validation in progress |
| **SUCCEEDED** | All steps completed without blocking errors |
| **FAILED** | One or more steps failed |
| **CANCELED** | Run was manually canceled |
| **TIMED_OUT** | Run exceeded the time limit |

### Watching Live Progress

In the UI, the run detail page updates automatically as steps complete. You'll see:

- Which step is currently running
- Completed steps with their pass/fail status
- Real-time findings as they're discovered

### Polling for Status (API)

When you start a run via API, you may receive:

- **201 Created**: The validation completed immediately. The response includes full results.
- **202 Accepted**: The validation is still running. Poll the provided URL to check status.

```bash
# Poll for status
curl -H "Authorization: Bearer $API_TOKEN" \
  "https://your-validibot.com/api/v1/orgs/my-org/runs/{run_id}/"
```

Keep polling until the status changes to a terminal state (SUCCEEDED, FAILED, CANCELED, or TIMED_OUT).

## Run Results

### Result Categories

Beyond the run status, each completed run has a **result** that summarizes the outcome:

| Result | Meaning |
|--------|---------|
| **PASS** | Validation succeeded with no blocking issues |
| **FAIL** | Validation completed but found errors |
| **ERROR** | Something went wrong during validation (system issue, not data issue) |
| **CANCELED** | Run was stopped before completion |
| **TIMED_OUT** | Run exceeded time limits |

### Understanding Findings

Findings are the individual issues discovered during validation. Each finding has:

- **Severity**: ERROR (blocks pass), WARNING (informational but doesn't block), INFO (purely informational)
- **Message**: What the validator found
- **Path**: Where in the data the issue was found (for structured data)
- **Step**: Which workflow step generated this finding

For more on interpreting findings, see [Reviewing Results](reviewing-results.md).

## Canceling a Run

If a validation is taking too long or you submitted the wrong file:

1. Go to the run detail page.
2. Click **Cancel** (available while the run is PENDING or RUNNING).

The run status changes to CANCELED. Any findings already recorded are preserved.

## Rerunning Validations

To run the same workflow against the same submission again:

1. Go to the completed run's detail page.
2. Click **Rerun**.

This creates a new run with the original submission. Useful after updating workflow rules to see if previously-failing data now passes.

## Troubleshooting Common Issues

**"Workflow is not active"**: The workflow is disabled. An admin needs to activate it before runs can be submitted.

**"No workflow steps"**: The workflow has no validation steps configured. Add at least one step.

**"File type unsupported"**: Your file format doesn't match what the workflow accepts. Check the workflow's allowed file types, or verify your `Content-Type` header in API requests.

**Run stuck in PENDING**: The task queue may be busy. If it doesn't start within a few minutes, check with your administrator.

For more troubleshooting help, see [Troubleshooting](troubleshooting.md).
