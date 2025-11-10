# Authentication

Lay out the steps teams should follow to secure API calls.

## Generate Tokens
Document where admins create API tokens in the UI, what scopes/roles are available, and how to store the token securely after download.

## Include Tokens in Requests
Show the standard `Authorization: Bearer <token>` header and mention how long tokens remain valid. Call out any IP restrictions or environment variables your team prefers.

## Rotate or Revoke Tokens
Explain how to replace a compromised token, what happens to in-flight jobs, and how revocation affects CLI sessions. Leave space for a checklist of people to notify after a rotation.

## Auditing Access
Provide guidance on reviewing token usage logs, setting up alerts, and mapping tokens back to owners.
