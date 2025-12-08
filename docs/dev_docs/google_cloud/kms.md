# Google Cloud KMS for Credential Signing

This document describes how Validibot uses Google Cloud Key Management Service (KMS) to cryptographically sign validation credentials (badges) issued to users.

## Overview

When a validation run completes successfully, Validibot can issue a signed credential (JWT badge) that proves:

1. A specific validation workflow was executed
2. The submitted data passed all validation steps
3. The credential was issued by Validibot (cryptographic signature)
4. The credential hasn't been tampered with

These credentials are signed using an asymmetric key pair stored in Google Cloud KMS. The private key never leaves Google's infrastructure, and we only use it to create signatures. The corresponding public key is published at `/.well-known/jwks.json` so anyone can verify our signatures.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Validation Flow                           │
│                                                              │
│  1. User submits data                                        │
│  2. Validation runs and completes successfully               │
│  3. Django app creates JWT payload (claims)                  │
│  4. Django app calls Google Cloud KMS to sign the JWT        │
│  5. Signed credential returned to user                       │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                 Verification Flow                            │
│                                                              │
│  1. Verifier receives credential from user                   │
│  2. Verifier fetches public keys from /.well-known/jwks.json │
│  3. Verifier validates signature using public key            │
│  4. Verifier checks claims (expiry, issuer, etc.)            │
└─────────────────────────────────────────────────────────────┘
```

## Google Cloud KMS Setup

### Key Hierarchy

Google Cloud KMS uses a hierarchical structure:

```
Project
  └── Location (australia-southeast1)
      └── KeyRing (validibot-keys)
          └── CryptoKey (credential-signing)
              └── CryptoKeyVersion (1, 2, 3...)
```

- **KeyRing**: A logical grouping of keys (one per environment)
- **CryptoKey**: The signing key (supports automatic rotation)
- **CryptoKeyVersion**: Each rotation creates a new version (old versions remain accessible for verification)

### Production Setup

#### 1. Create KeyRing (One-Time Setup)

```bash
# Create keyring in Australian region
gcloud kms keyrings create validibot-keys \
  --location australia-southeast1 \
  --project PROJECT_ID
```

KeyRings cannot be deleted, so use descriptive names.

#### 2. Create Asymmetric Signing Key

```bash
# Create EC P-256 signing key (ES256 algorithm)
gcloud kms keys create credential-signing \
  --location australia-southeast1 \
  --keyring validibot-keys \
  --purpose asymmetric-signing \
  --default-algorithm ec-sign-p256-sha256 \
  --protection-level software \
  --rotation-period 90d \
  --next-rotation-time $(date -u -d "+90 days" +%Y-%m-%dT%H:%M:%SZ)
```

**Key Parameters:**

- `asymmetric-signing`: Key is used for digital signatures (not encryption)
- `ec-sign-p256-sha256`: Elliptic Curve P-256 with SHA-256 (produces ES256 JWTs)
- `software`: Key stored in software (HSM also available for higher security)
- `rotation-period 90d`: Automatically create new key version every 90 days
- `next-rotation-time`: When the first rotation should occur

**Why EC P-256 (ES256)?**

- Smaller signatures than RSA (256 bits vs 2048+ bits)
- Widely supported by JWT libraries
- Industry standard for JWTs
- Good performance

#### 3. Grant Cloud Run Service Account Access

The Cloud Run service needs permission to:

- Get the public key (for publishing in JWKS)
- Use the private key to sign (asymmetric signing)

```bash
# Grant permissions to production service account
gcloud kms keys add-iam-policy-binding credential-signing \
  --location australia-southeast1 \
  --keyring validibot-keys \
  --member serviceAccount:validibot-prod-app@PROJECT_ID.iam.gserviceaccount.com \
  --role roles/cloudkms.viewer

gcloud kms keys add-iam-policy-binding credential-signing \
  --location australia-southeast1 \
  --keyring validibot-keys \
  --member serviceAccount:validibot-prod-app@PROJECT_ID.iam.gserviceaccount.com \
  --role roles/cloudkms.signerVerifier
```

**Roles:**

- `cloudkms.viewer`: Can list keys and get public keys
- `cloudkms.signerVerifier`: Can sign data and verify signatures

#### 4. Verify Setup

```bash
# List keys in keyring
gcloud kms keys list \
  --location australia-southeast1 \
  --keyring validibot-keys

# Get public key (as PEM)
gcloud kms keys versions get-public-key 1 \
  --key credential-signing \
  --location australia-southeast1 \
  --keyring validibot-keys \
  --output-file public-key.pem
```

### Development Setup

For local development and staging:

```bash
# Create separate keyring for development
gcloud kms keyrings create validibot-keys-dev \
  --location australia-southeast1 \
  --project PROJECT_ID

# Create dev signing key (no rotation needed for dev)
gcloud kms keys create credential-signing-dev \
  --location australia-southeast1 \
  --keyring validibot-keys-dev \
  --purpose asymmetric-signing \
  --default-algorithm ec-sign-p256-sha256 \
  --protection-level software

# Grant dev service account access
gcloud kms keys add-iam-policy-binding credential-signing-dev \
  --location australia-southeast1 \
  --keyring validibot-keys-dev \
  --member serviceAccount:validibot-dev-app@PROJECT_ID.iam.gserviceaccount.com \
  --role roles/cloudkms.viewer

gcloud kms keys add-iam-policy-binding credential-signing-dev \
  --location australia-southeast1 \
  --keyring validibot-keys-dev \
  --member serviceAccount:validibot-dev-app@PROJECT_ID.iam.gserviceaccount.com \
  --role roles/cloudkms.signerVerifier
```

## Environment Configuration

### Production Environment Variables

Set these in Google Secret Manager or Cloud Run environment:

```bash
# Primary signing key (resource name format)
GCP_KMS_SIGNING_KEY="projects/PROJECT_ID/locations/australia-southeast1/keyRings/validibot-keys/cryptoKeys/credential-signing"

# Keys to publish in JWKS (comma-separated, supports multiple for rotation)
GCP_KMS_JWKS_KEYS="projects/PROJECT_ID/locations/australia-southeast1/keyRings/validibot-keys/cryptoKeys/credential-signing"

# Algorithm advertised in JWKS
SV_JWKS_ALG="ES256"

# Google Cloud project and location
GCP_PROJECT_ID="your-project-id"
GCP_LOCATION="australia-southeast1"
```

### Django Settings

In `config/settings/production.py`:

```python
# KMS Configuration
GCP_KMS_SIGNING_KEY = env(
    "GCP_KMS_SIGNING_KEY",
    default="projects/PROJECT_ID/locations/australia-southeast1/keyRings/validibot-keys/cryptoKeys/credential-signing",
)

GCP_KMS_JWKS_KEYS = env.list(
    "GCP_KMS_JWKS_KEYS",
    default=[GCP_KMS_SIGNING_KEY],
)

SV_JWKS_ALG = env("SV_JWKS_ALG", default="ES256")
```

### Local Development

For local development, you have two options:

**Option 1: Use Real KMS (Recommended)**

Authenticate with Google Cloud and use the dev key:

```bash
# Authenticate
gcloud auth application-default login

# Set environment variables
export GCP_KMS_SIGNING_KEY="projects/PROJECT_ID/locations/australia-southeast1/keyRings/validibot-keys-dev/cryptoKeys/credential-signing-dev"
export GCP_KMS_JWKS_KEYS="projects/PROJECT_ID/locations/australia-southeast1/keyRings/validibot-keys-dev/cryptoKeys/credential-signing-dev"
export SV_JWKS_ALG="ES256"
```

**Option 2: Mock for Offline Development**

Set empty JWKS keys to skip KMS calls:

```bash
export GCP_KMS_JWKS_KEYS=""
```

The JWKS endpoint will return an empty key set. Tests use mocks so they don't require real KMS.

## Key Rotation

Google Cloud KMS supports automatic key rotation:

### Automatic Rotation

When a key rotates:

1. Google creates a new CryptoKeyVersion
2. New signatures use the new version
3. Old versions remain active for verification
4. JWKS endpoint publishes ALL active versions

Our rotation policy:

- **Production**: Every 90 days
- **Development**: Manual only

### Manual Rotation

To manually create a new key version:

```bash
# Create new version (becomes primary automatically)
gcloud kms keys versions create \
  --key credential-signing \
  --location australia-southeast1 \
  --keyring validibot-keys \
  --primary
```

### Rotation Best Practices

1. **Before Rotation**: Ensure JWKS publishes all versions
2. **During Rotation**: Both old and new keys in JWKS
3. **After Rotation**: Keep old key for at least 90 days (credential lifetime)
4. **Monitoring**: Alert if JWKS fetch fails

### Publishing Multiple Keys During Rotation

Set `GCP_KMS_JWKS_KEYS` to include both the current and previous key:

```bash
# Comma-separated list of key resource names
export GCP_KMS_JWKS_KEYS="projects/PROJECT_ID/locations/australia-southeast1/keyRings/validibot-keys/cryptoKeys/credential-signing,projects/PROJECT_ID/locations/australia-southeast1/keyRings/validibot-keys/cryptoKeys/credential-signing-old"
```

## JWKS Endpoint

### Endpoint Details

- **URL**: `https://app.validibot.com/.well-known/jwks.json`
- **Content-Type**: `application/jwk-set+json`
- **Caching**: Recommended 1 hour (verifiers should cache)

### Example Response

```json
{
  "keys": [
    {
      "kty": "EC",
      "use": "sig",
      "alg": "ES256",
      "kid": "a7b3c8d9...",
      "crv": "P-256",
      "x": "base64url-encoded-x-coordinate",
      "y": "base64url-encoded-y-coordinate"
    }
  ]
}
```

### Key Fields

- `kty`: Key type (EC for elliptic curve)
- `use`: Usage (sig for signature)
- `alg`: Algorithm (ES256)
- `kid`: Key ID (SHA-256 hash of DER public key)
- `crv`: Curve name (P-256)
- `x`, `y`: Public key coordinates

## Signing Process

### 1. Create JWT Claims

```python
import time
from validibot.validations.models import ValidationRun

def create_credential_claims(validation_run: ValidationRun) -> dict:
    """Create JWT claims for a validation credential."""
    now = int(time.time())

    return {
        "iss": "https://app.validibot.com",  # Issuer
        "sub": f"validation:{validation_run.id}",  # Subject
        "aud": "https://verifier.example.com",  # Audience (optional)
        "exp": now + (90 * 24 * 60 * 60),  # Expiry (90 days)
        "iat": now,  # Issued at
        "nbf": now,  # Not before
        # Custom claims
        "validation_run_id": validation_run.id,
        "workflow": validation_run.workflow.slug,
        "workflow_version": validation_run.workflow.version,
        "status": "passed",
    }
```

### 2. Sign with Google Cloud KMS

```python
from google.cloud import kms
from django.conf import settings
import base64
import json

def sign_credential(claims: dict) -> str:
    """Sign JWT claims using Google Cloud KMS."""

    # Create JWT header
    header = {
        "alg": "ES256",
        "typ": "JWT",
        "kid": get_key_id(),  # From JWKS
    }

    # Encode header and payload
    header_b64 = base64url_encode(json.dumps(header))
    payload_b64 = base64url_encode(json.dumps(claims))
    message = f"{header_b64}.{payload_b64}"

    # Sign with KMS
    client = kms.KeyManagementServiceClient()

    # Calculate digest (KMS requires pre-hashed message for EC keys)
    import hashlib
    digest = hashlib.sha256(message.encode()).digest()

    response = client.asymmetric_sign(
        request={
            "name": settings.GCP_KMS_SIGNING_KEY + "/cryptoKeyVersions/1",
            "digest": {"sha256": digest},
        }
    )

    # Encode signature
    signature_b64 = base64url_encode(response.signature)

    return f"{message}.{signature_b64}"
```

## Verification Process

Verifiers (external parties) verify credentials:

### 1. Fetch JWKS

```python
import requests

def fetch_jwks():
    """Fetch public keys from Validibot."""
    response = requests.get("https://app.validibot.com/.well-known/jwks.json")
    return response.json()["keys"]
```

### 2. Verify Signature

```python
import jwt

def verify_credential(token: str) -> dict:
    """Verify a Validibot credential."""

    # Fetch public keys
    jwks_client = jwt.PyJWKClient("https://app.validibot.com/.well-known/jwks.json")
    signing_key = jwks_client.get_signing_key_from_jwt(token)

    # Verify and decode
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["ES256"],
        issuer="https://app.validibot.com",
        options={"verify_exp": True},
    )

    return claims
```

## Security Considerations

### Key Security

1. **Private Key Never Leaves KMS**: Google Cloud KMS stores private keys in FIPS 140-2 Level 3 validated HSMs
2. **Audit Logging**: All KMS operations logged to Cloud Audit Logs
3. **IAM Controls**: Service account permissions tightly scoped
4. **Regional Isolation**: Keys stored in Australian region only

### JWT Security

1. **Short Expiry**: Credentials expire in 90 days
2. **Audience Validation**: Verifiers should check `aud` claim
3. **Issuer Validation**: Always verify `iss` is `https://app.validibot.com`
4. **Algorithm Whitelist**: Only accept ES256, reject `none` algorithm

### Operational Security

1. **Key Rotation**: Automatic 90-day rotation
2. **Monitoring**: Alert on KMS API failures
3. **Access Reviews**: Quarterly review of KMS IAM bindings
4. **Incident Response**: Procedure to revoke compromised keys

## Monitoring and Alerts

### Cloud Monitoring Metrics

Monitor these KMS metrics:

```bash
# Request count
cloudkms.googleapis.com/api/request_count

# Request latency
cloudkms.googleapis.com/api/request_latencies

# Failed requests
cloudkms.googleapis.com/api/request_count (filtered by response_code >= 400)
```

### Recommended Alerts

1. **KMS API Failure Rate > 5%**: Alert if signing requests fail
2. **JWKS Fetch Latency > 2s**: Alert if public key fetch is slow
3. **Unauthorized Access Attempts**: Alert on 403 errors in Cloud Audit Logs

### Audit Logging

Review Cloud Audit Logs for:

- Who accessed KMS keys (service accounts)
- When keys were used for signing
- Any failed authorization attempts

```bash
# View KMS audit logs
gcloud logging read "resource.type=cloudkms_cryptokey" \
  --limit 50 \
  --format json
```

## Cost

Google Cloud KMS pricing (as of 2025):

- **Key Versions**: $0.06 per key version per month
- **Signing Operations**: $0.03 per 10,000 operations
- **Public Key Retrieval**: Free

**Example Monthly Cost**:

- 2 active key versions: $0.12/month
- 10,000 credentials issued: $0.03/month
- **Total**: ~$0.15/month

Significantly cheaper than AWS KMS ($1/key/month + $0.03/10k operations).

## Migration from AWS KMS

See [ADR: AWS KMS to Google Cloud KMS Migration](../../adr/2025-12-04-kms-migration.md) for complete migration plan.

**Key Steps**:

1. Create Google Cloud KMS key
2. Update Django code to use Google Cloud KMS SDK
3. Deploy with both AWS and Google keys in JWKS (transition period)
4. Switch to Google Cloud KMS for new signatures
5. Remove AWS KMS after 90 days (credential expiry period)

## Troubleshooting

### Issue: JWKS Endpoint Returns Empty Keys

**Cause**: `GCP_KMS_JWKS_KEYS` not set or service account lacks permissions

**Solution**:

```bash
# Verify environment variable
echo $GCP_KMS_JWKS_KEYS

# Check IAM permissions
gcloud kms keys get-iam-policy credential-signing \
  --location australia-southeast1 \
  --keyring validibot-keys
```

### Issue: Signing Fails with "Permission Denied"

**Cause**: Service account doesn't have `cloudkms.signerVerifier` role

**Solution**:

```bash
gcloud kms keys add-iam-policy-binding credential-signing \
  --location australia-southeast1 \
  --keyring validibot-keys \
  --member serviceAccount:YOUR-SA@PROJECT.iam.gserviceaccount.com \
  --role roles/cloudkms.signerVerifier
```

### Issue: Verifiers Can't Verify Signatures

**Cause**: Public key not in JWKS or wrong algorithm

**Solution**:

1. Check JWKS endpoint manually: `curl https://app.validibot.com/.well-known/jwks.json`
2. Verify `kid` in JWT header matches a key in JWKS
3. Ensure verifier accepts ES256 algorithm

## References

- [Google Cloud KMS Documentation](https://cloud.google.com/kms/docs)
- [JSON Web Key (JWK) RFC 7517](https://datatracker.ietf.org/doc/html/rfc7517)
- [JSON Web Signature (JWS) RFC 7515](https://datatracker.ietf.org/doc/html/rfc7515)
- [JWT Best Practices RFC 8725](https://datatracker.ietf.org/doc/html/rfc8725)
