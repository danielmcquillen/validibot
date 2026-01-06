# Running Validations

Outline the different ways operators can start and monitor workflow runs.

## Launch From the Dashboard
Describe the workflow launch screen, required inputs, and how to choose the default project. Reserve space for screenshots showing the Run tab and the submission form.

## Launch With Uploaded Files
Document size limits, supported formats, and tips for preparing files before upload. Note any automatic metadata Validibot captures from filenames.

## Launch Through the API
Summarize the REST endpoint for `POST /api/v1/orgs/{org_slug}/workflows/{workflow_identifier}/runs/` and reference the detailed API sections for payload formats. Mention how role requirements (EXECUTOR) are checked before a run starts.

## Monitor Progress
Explain the live status indicators, HTMX-powered updates, and the difference between queued, running, succeeded, and failed states. Leave space for describing the retry and cancellation options if applicable.
