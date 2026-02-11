# Webhooks & Notifications

Webhooks allow external systems to receive real-time notifications when validation events occur. Instead of polling for status changes, your systems can react immediately when runs complete.

## When to Use Webhooks

Webhooks are ideal for:

**CI/CD Integration**: Notify your pipeline when validations complete, allowing subsequent steps to proceed or fail based on results.

**Alerting**: Send notifications to Slack, email, or monitoring systems when validations fail.

**Data Synchronization**: Update external databases or dashboards with validation outcomes.

**Workflow Automation**: Trigger downstream processes like certificate generation or approval workflows.

### Webhooks vs Polling

| Approach | Best For |
|----------|----------|
| **Webhooks** | Real-time reactions, reducing API calls, event-driven architectures |
| **Polling** | Simple integrations, environments where receiving webhooks is difficult |

If you can receive incoming HTTP requests, webhooks are generally more efficient than polling.

## Configuring Webhooks

Webhooks are configured at the workflow level. To set up a webhook:

1. Open the workflow you want to monitor
2. Go to the workflow settings or add a webhook action step
3. Configure the webhook endpoint:
   - **URL**: The HTTPS endpoint that will receive notifications
   - **Events**: Which events trigger the webhook (run completed, run failed, etc.)
   - **Secret**: A shared secret for verifying webhook signatures (recommended)

### Endpoint Requirements

Your webhook endpoint must:

- Accept POST requests
- Respond with a 2xx status code within 30 seconds
- Use HTTPS (HTTP endpoints may be rejected)

### Testing Your Endpoint

Before configuring in Validibot, verify your endpoint works:

```bash
# Test with curl
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"test": true}' \
  "https://your-server.com/webhook-endpoint"
```

## Webhook Payload

When an event occurs, Validibot sends a POST request with a JSON payload:

```json
{
  "event": "run.completed",
  "timestamp": "2024-01-15T14:30:00Z",
  "organization": {
    "slug": "my-org",
    "name": "My Organization"
  },
  "workflow": {
    "id": 42,
    "slug": "product-validation",
    "name": "Product Validation"
  },
  "run": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "SUCCEEDED",
    "result": "PASS",
    "started_at": "2024-01-15T14:29:55Z",
    "ended_at": "2024-01-15T14:30:00Z",
    "duration_ms": 5000,
    "url": "https://your-validibot.com/orgs/my-org/runs/550e8400-e29b-41d4-a716-446655440000/"
  },
  "summary": {
    "total_findings": 3,
    "error_count": 0,
    "warning_count": 2,
    "info_count": 1
  }
}
```

### Event Types

| Event | Description |
|-------|-------------|
| `run.completed` | A validation run finished (any outcome) |
| `run.succeeded` | A validation run passed |
| `run.failed` | A validation run failed |

### Payload Fields

| Field | Description |
|-------|-------------|
| `event` | The event type that triggered the webhook |
| `timestamp` | When the event occurred (ISO 8601) |
| `organization` | The organization containing the workflow |
| `workflow` | The workflow that was run |
| `run` | Details about the validation run |
| `summary` | Aggregated finding counts |

## Verifying Webhook Signatures

To ensure webhooks come from Validibot (not an attacker), verify the signature:

1. Validibot includes an `X-Validibot-Signature` header with each request
2. The signature is an HMAC-SHA256 of the request body using your webhook secret
3. Verify the signature before processing the payload

### Verification Example (Python)

```python
import hmac
import hashlib

def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)

# In your webhook handler
@app.route("/webhook", methods=["POST"])
def handle_webhook():
    signature = request.headers.get("X-Validibot-Signature")
    if not verify_signature(request.data, signature, WEBHOOK_SECRET):
        return "Invalid signature", 401

    data = request.json
    # Process the webhook...
```

### Verification Example (Node.js)

```javascript
const crypto = require('crypto');

function verifySignature(payload, signature, secret) {
  const expected = 'sha256=' + crypto
    .createHmac('sha256', secret)
    .update(payload)
    .digest('hex');
  return crypto.timingSafeEqual(
    Buffer.from(signature),
    Buffer.from(expected)
  );
}
```

## Handling Webhook Delivery

### Responding to Webhooks

Your endpoint should:

1. Respond quickly (within 30 seconds)
2. Return a 2xx status code to acknowledge receipt
3. Process the payload asynchronously if needed

```python
@app.route("/webhook", methods=["POST"])
def handle_webhook():
    # Verify signature first
    # ...

    # Queue for async processing
    queue.enqueue(process_validation_result, request.json)

    # Respond immediately
    return "", 200
```

### Retry Behavior

If your endpoint doesn't respond with 2xx:

- Validibot retries with exponential backoff
- Retries occur at approximately 1, 5, 30, and 60 minutes
- After multiple failures, the webhook is marked as failing

### Idempotency

Webhooks may be delivered more than once. Design your handler to be idempotent:

```python
def process_webhook(data):
    run_id = data["run"]["id"]

    # Check if already processed
    if already_processed(run_id):
        return

    # Process and mark as handled
    do_processing(data)
    mark_processed(run_id)
```

## Testing Webhooks

### Local Development

For local testing, use a tunneling service like ngrok:

```bash
# Start ngrok
ngrok http 5000

# Use the ngrok URL in your webhook configuration
# https://abc123.ngrok.io/webhook
```

### Request Inspection

Use services like RequestBin or Webhook.site to inspect webhook payloads before implementing your handler.

### Manual Testing

Trigger a test validation to verify the webhook fires:

1. Configure the webhook on a test workflow
2. Run a validation
3. Check that your endpoint received the request
4. Verify the payload structure matches expectations

## Monitoring Webhook Health

### Delivery Status

In the Validibot UI, you can view webhook delivery status:

- Recent delivery attempts
- Success/failure rates
- Response codes from your endpoint

### Alerting on Failures

Set up monitoring for your webhook endpoint to catch issues:

- Response time degradation
- Error rate increases
- Connectivity problems

## Troubleshooting

### Not Receiving Webhooks

1. **Check the URL**: Ensure the configured URL is correct and accessible from the internet
2. **Verify HTTPS**: Most deployments require HTTPS endpoints
3. **Check firewall rules**: Your endpoint must accept incoming connections
4. **Review logs**: Check Validibot's webhook delivery logs for error details

### Signature Verification Failing

1. **Check the secret**: Ensure you're using the exact secret configured in Validibot
2. **Use raw body**: Verify signatures against the raw request body, not parsed JSON
3. **Check encoding**: Ensure consistent string encoding (UTF-8)

### Timeouts

If your processing takes too long:

1. Respond with 200 immediately
2. Process the payload asynchronously (queue, background job)
3. Implement proper error handling for async processing
