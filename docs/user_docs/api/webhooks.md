# Webhooks & Notifications

Use this template to explain how downstream systems can react to workflow activity.

## When to Use Webhooks
Describe common scenarios (syncing results to CRMs, triggering certificate workflows, updating ticketing systems). Note how webhooks compare to polling the run status endpoint.

## Configure Endpoints
List the settings users must supply (URL, secret, retry policy). Leave placeholders for screenshots of the configuration screen and any allowlist requirements.

## Payload Structure
Summarize the JSON fields sent in each webhook (run ID, workflow name, status, findings summary). Include space for sample payloads and signature verification instructions.

## Testing and Monitoring
Provide steps for using tools like ngrok or RequestBin, plus tips for monitoring delivery status and retry attempts from the SimpleValidations UI.
