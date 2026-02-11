# API Authentication

Validibot uses Bearer authentication for API access. This page covers how to obtain and use API keys.

## Getting an API Key

1. Log in to the Validibot web app
2. Go to **Settings → API Key**
3. Click **Generate API Key**
4. Copy the key immediately - it's only shown once

## Using Your API Key

Include the key in the `Authorization` header with the `Bearer` prefix:

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" \
  https://app.validibot.com/api/v1/auth/me/
```

## Endpoints

### Verify API Key / Get Current User

Use this endpoint to validate your API key and retrieve basic user info:

```
GET /api/v1/auth/me/
```

**Response (200 OK):**

```json
{
  "email": "user@example.com",
  "name": "User Name"
}
```

**Response (403 Forbidden):**

```json
{
  "detail": "Authentication credentials were not provided."
}
```

This endpoint is intentionally minimal - it only returns the email and display name. This follows security best practices by not exposing unnecessary user data through the API.

## API Key Storage (CLI)

The [Validibot CLI](https://github.com/danielmcquillen/validibot-cli) stores API keys securely:

- **macOS**: Keychain
- **Windows**: Credential Manager
- **Linux**: Secret Service (via libsecret)

If the system keyring is unavailable, the CLI falls back to a file at `~/.config/validibot/credentials.json` with restrictive permissions (600).

## Environment Variable

For CI/CD and scripting, you can set the API key via environment variable:

```bash
export VALIDIBOT_TOKEN="your-api-key"
```

The CLI checks for this variable before looking in the keyring or credentials file.

## Related

- [Example Client](./example-client.py) - Python script demonstrating API usage
- [OpenAPI Schema](/api/v1/schema/) - Full API documentation (when running locally)
