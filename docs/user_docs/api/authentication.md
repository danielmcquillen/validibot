# Authentication

All Validibot API requests require authentication. This guide covers how to create, use, and manage API tokens.

## Creating API Tokens

To create an API token:

1. Log into Validibot
2. Click your avatar in the top-right corner
3. Select **Settings** or **Profile**
4. Navigate to the **API Tokens** section
5. Click **Create Token**
6. Give the token a descriptive name (e.g., "CI/CD Pipeline", "Local Development")
7. Click **Create**
8. **Copy the token immediately** — you won't be able to see it again

Store the token securely. Treat it like a password.

### Token Permissions

API tokens inherit the permissions of the user who created them. If you have Author access to an organization, tokens you create will have Author access.

For service accounts or automation, consider creating a dedicated user with only the permissions needed (typically Executor).

## Using Tokens in Requests

Include your token in the `Authorization` header using the Bearer scheme:

```bash
curl -H "Authorization: Bearer YOUR_API_TOKEN" \
  "https://your-validibot.com/api/v1/orgs/my-org/workflows/"
```

### Environment Variables

For scripts and automation, store your token in an environment variable:

```bash
# Set the token
export VALIDIBOT_TOKEN="your-token-here"

# Use it in requests
curl -H "Authorization: Bearer $VALIDIBOT_TOKEN" \
  "https://your-validibot.com/api/v1/orgs/my-org/workflows/"
```

Never hardcode tokens in source code or commit them to version control.

### Common Integration Patterns

**Shell scripts**:

```bash
#!/bin/bash
API_TOKEN="${VALIDIBOT_TOKEN:?VALIDIBOT_TOKEN environment variable not set}"
API_BASE="https://your-validibot.com/api/v1"

curl -X POST \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @data.json \
  "$API_BASE/orgs/my-org/workflows/my-workflow/runs/"
```

**Python**:

```python
import os
import requests

token = os.environ["VALIDIBOT_TOKEN"]
headers = {"Authorization": f"Bearer {token}"}

response = requests.post(
    "https://your-validibot.com/api/v1/orgs/my-org/workflows/my-workflow/runs/",
    headers=headers,
    json={"content": "..."}
)
```

**CI/CD (GitHub Actions)**:

```yaml
steps:
  - name: Run validation
    env:
      VALIDIBOT_TOKEN: ${{ secrets.VALIDIBOT_TOKEN }}
    run: |
      curl -X POST \
        -H "Authorization: Bearer $VALIDIBOT_TOKEN" \
        -H "Content-Type: application/json" \
        --data-binary @config.json \
        "https://your-validibot.com/api/v1/orgs/my-org/workflows/config-check/runs/"
```

## Token Expiration

By default, API tokens don't expire. However, your administrator may configure:

- Automatic expiration after a certain period
- Maximum token lifetime
- Required rotation policies

Check with your administrator for your organization's token policies.

## Revoking Tokens

If a token is compromised or no longer needed:

1. Go to **Settings** → **API Tokens**
2. Find the token to revoke
3. Click **Revoke** or the delete icon
4. Confirm the revocation

Revoked tokens stop working immediately. Any in-flight requests using that token will fail.

### What Happens When You Revoke a Token

- Active API requests fail with `401 Unauthorized`
- Scheduled jobs using the token stop working
- CLI tools configured with the token need reconfiguration

Before revoking, ensure you have replacement tokens configured for any critical automation.

## Rotating Tokens

To rotate a token without downtime:

1. Create a new token
2. Update your applications to use the new token
3. Verify the new token works
4. Revoke the old token

For critical systems, run both tokens in parallel briefly to ensure continuity.

### Rotation Best Practices

- Schedule regular rotations (e.g., quarterly)
- Document where each token is used
- Use descriptive token names that indicate purpose and creation date
- Consider using short-lived tokens for sensitive operations

## Troubleshooting Authentication

### "Authentication required" (401)

**Possible causes**:

- Token not included in request
- Token format incorrect (missing "Bearer " prefix)
- Token has been revoked
- Token was created by a user who has been removed

**Solutions**:

1. Verify the `Authorization` header is present and correctly formatted
2. Check that the token hasn't been revoked
3. Try creating a new token

### "Permission denied" (403)

**Possible causes**:

- Token's user lacks required permissions
- User has been removed from the organization
- Organization has been deleted or suspended

**Solutions**:

1. Verify the token's user has the required role (e.g., Executor for running validations)
2. Check that the user still has access to the target organization
3. Contact an administrator to verify organization status

### Testing Your Token

To verify a token works:

```bash
curl -H "Authorization: Bearer $VALIDIBOT_TOKEN" \
  "https://your-validibot.com/api/v1/me/"
```

This should return information about the authenticated user. If it fails, the token is invalid or has been revoked.

## Security Best Practices

**Do**:

- Store tokens in secure secrets management (environment variables, vault systems)
- Use different tokens for different environments (dev, staging, production)
- Rotate tokens regularly
- Use tokens with minimum required permissions
- Monitor token usage for anomalies

**Don't**:

- Commit tokens to version control
- Share tokens between users or teams
- Use personal tokens for automated systems
- Keep tokens longer than necessary
- Ignore failed authentication attempts (they may indicate compromise)
