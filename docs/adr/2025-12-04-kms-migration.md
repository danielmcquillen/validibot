# ADR-2025-12-04: Migrate from AWS KMS to Google Cloud KMS

**Status:** In Progress
**Date:** 2025-12-04
**Owner:** Daniel / Validibot Platform
**Related ADRs:** 2025-12-02 Heroku to GCP Migration

---

## Context

As part of our migration from Heroku to Google Cloud Platform, we need to migrate our cryptographic signing infrastructure from AWS KMS to Google Cloud KMS.

### Current State

We currently use **AWS KMS** to sign validation credentials (JWT badges):

- **AWS KMS Key**: Multi-region key with alias `alias/sv-credential-signing-prod`
- **Algorithm**: ECDSA with P-256 curve (ES256 JWTs)
- **Region**: `us-west-1` (primary), replicated to `ap-southeast-2`
- **Code**: `validibot/core/jwks.py` uses `boto3` to interact with AWS KMS
- **JWKS Endpoint**: `/.well-known/jwks.json` publishes public keys for signature verification

### Why Migrate?

1. **Data Sovereignty**: Moving signing keys to Australian region (`australia-southeast1`) aligns with our data residency goals
2. **Unified Infrastructure**: Consolidate all infrastructure on GCP, simplifying operations and billing
3. **Cost Savings**: Google Cloud KMS is ~94% cheaper than AWS KMS ($0.06/key/month vs $1/key/month)
4. **Native Integration**: Better integration with Cloud Run via Application Default Credentials
5. **Remove AWS Dependency**: Eliminate last remaining AWS service dependency

---

## Decision

We will **completely replace** AWS KMS with Google Cloud KMS for all credential signing operations.

### Migration Strategy

**Gradual cutover with dual-key publication**:

1. Create Google Cloud KMS key
2. Update code to support both AWS and Google Cloud KMS
3. Publish BOTH keys in JWKS endpoint (transition period)
4. Switch to Google Cloud KMS for new signatures
5. Keep AWS key in JWKS for 90 days (credential lifetime)
6. Remove AWS KMS code and dependencies

---

## Implementation Plan

### Phase 1: Google Cloud Infrastructure Setup

**Timeline**: Day 1

#### Step 1.1: Create Production KeyRing and Key

```bash
# Set project
export GCP_PROJECT_ID="your-project-id"
gcloud config set project $GCP_PROJECT_ID

# Create keyring (production)
gcloud kms keyrings create validibot-keys \
  --location australia-southeast1

# Create asymmetric signing key
gcloud kms keys create credential-signing \
  --location australia-southeast1 \
  --keyring validibot-keys \
  --purpose asymmetric-signing \
  --default-algorithm ec-sign-p256-sha256 \
  --protection-level software \
  --rotation-period 90d \
  --next-rotation-time $(date -u -d "+90 days" +%Y-%m-%dT%H:%M:%SZ)

# Verify key created
gcloud kms keys list \
  --location australia-southeast1 \
  --keyring validibot-keys
```

#### Step 1.2: Grant Cloud Run Service Account Access

```bash
# Get service account email
export SA_EMAIL="validibot-prod-app@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

# Grant viewer permission (can list keys, get public keys)
gcloud kms keys add-iam-policy-binding credential-signing \
  --location australia-southeast1 \
  --keyring validibot-keys \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/cloudkms.viewer

# Grant signer permission (can sign with private key)
gcloud kms keys add-iam-policy-binding credential-signing \
  --location australia-southeast1 \
  --keyring validibot-keys \
  --member "serviceAccount:${SA_EMAIL}" \
  --role roles/cloudkms.signerVerifier

# Verify permissions
gcloud kms keys get-iam-policy credential-signing \
  --location australia-southeast1 \
  --keyring validibot-keys
```

#### Step 1.3: Test Key Access

```bash
# Get public key (verify service account has access)
gcloud kms keys versions get-public-key 1 \
  --key credential-signing \
  --location australia-southeast1 \
  --keyring validibot-keys \
  --output-file /tmp/gcp-public-key.pem

# View the public key
cat /tmp/gcp-public-key.pem
```

#### Step 1.4: Create Development KeyRing and Key

```bash
# Create dev keyring
gcloud kms keyrings create validibot-keys-dev \
  --location australia-southeast1

# Create dev signing key (no rotation for dev)
gcloud kms keys create credential-signing-dev \
  --location australia-southeast1 \
  --keyring validibot-keys-dev \
  --purpose asymmetric-signing \
  --default-algorithm ec-sign-p256-sha256 \
  --protection-level software

# Grant dev service account access
export DEV_SA_EMAIL="validibot-dev-app@${GCP_PROJECT_ID}.iam.gserviceaccount.com"

gcloud kms keys add-iam-policy-binding credential-signing-dev \
  --location australia-southeast1 \
  --keyring validibot-keys-dev \
  --member "serviceAccount:${DEV_SA_EMAIL}" \
  --role roles/cloudkms.viewer

gcloud kms keys add-iam-policy-binding credential-signing-dev \
  --location australia-southeast1 \
  --keyring validibot-keys-dev \
  --member "serviceAccount:${DEV_SA_EMAIL}" \
  --role roles/cloudkms.signerVerifier
```

**Deliverables**:

- ✅ Production KMS key created in `australia-southeast1`
- ✅ Development KMS key created
- ✅ Service accounts granted appropriate IAM roles
- ✅ Public key accessible

---

### Phase 2: Code Migration

**Timeline**: Day 1-2

#### Step 2.1: Update Dependencies

**File**: `pyproject.toml`

```toml
# Remove AWS dependencies
# DELETE: boto3 = "^1.35.0"

# Add Google Cloud KMS
dependencies = [
    # ... existing deps ...
    "google-cloud-kms>=2.23.0",
]
```

Run:

```bash
uv lock
uv sync
```

#### Step 2.2: Create New Google Cloud KMS Module

**File**: `validibot/core/gcp_kms.py` (NEW)

```python
"""Google Cloud KMS integration for credential signing."""

import base64
import hashlib
from functools import lru_cache

from authlib.jose import JsonWebKey
from cryptography.hazmat.primitives import serialization
from django.conf import settings
from google.cloud import kms


def _kms_client():
    """Get Google Cloud KMS client."""
    return kms.KeyManagementServiceClient()


@lru_cache(maxsize=64)
def get_public_key_der(key_resource_name: str) -> bytes:
    """
    Fetch public key from Google Cloud KMS.

    Args:
        key_resource_name: Full resource name like
            "projects/PROJECT/locations/LOCATION/keyRings/RING/cryptoKeys/KEY"

    Returns:
        Public key in DER format
    """
    client = _kms_client()

    # Get the latest version (or specify version number)
    # For now, we'll use the latest by appending /cryptoKeyVersions/1
    # In production, consider querying for the primary version
    key_version_name = f"{key_resource_name}/cryptoKeyVersions/1"

    response = client.get_public_key(request={"name": key_version_name})

    # Parse PEM to get DER
    public_key = serialization.load_pem_public_key(response.pem.encode())
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    return der


def _b64url(b: bytes) -> str:
    """Base64url encode bytes without padding."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def kid_from_der(der: bytes) -> str:
    """
    Generate key ID from DER public key.

    Uses SHA-256 thumbprint for stable, unique kid.
    """
    return _b64url(hashlib.sha256(der).digest())


def jwk_from_gcp_key(key_resource_name: str, alg: str) -> dict:
    """
    Convert Google Cloud KMS key to JWK format.

    Args:
        key_resource_name: Full KMS key resource name
        alg: Algorithm (e.g., "ES256")

    Returns:
        JWK dictionary
    """
    der = get_public_key_der(key_resource_name)

    # Load DER and convert to PEM
    public_key = serialization.load_der_public_key(der)
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Use authlib to convert PEM to JWK
    public_jwk = JsonWebKey.import_key(pem).as_dict()

    # Add JWK metadata
    public_jwk["use"] = "sig"
    public_jwk["alg"] = alg
    public_jwk["kid"] = kid_from_der(der)

    return public_jwk


def sign_data(key_resource_name: str, data: bytes) -> bytes:
    """
    Sign data using Google Cloud KMS.

    Args:
        key_resource_name: Full KMS key resource name
        data: Data to sign

    Returns:
        Signature bytes
    """
    client = _kms_client()

    # EC keys require pre-hashed message
    digest = hashlib.sha256(data).digest()

    # Get latest version
    key_version_name = f"{key_resource_name}/cryptoKeyVersions/1"

    response = client.asymmetric_sign(
        request={
            "name": key_version_name,
            "digest": {"sha256": digest},
        }
    )

    return response.signature
```

#### Step 2.3: Update JWKS Module to Support Both Providers

**File**: `validibot/core/jwks.py`

```python
# core/jwks.py
"""
JWKS (JSON Web Key Set) endpoint support.

During migration, supports both AWS KMS and Google Cloud KMS.
"""

from django.conf import settings

# Choose your advertised alg to match your KMS key:
# ES256 for ECC_NIST_P256, PS256 for RSA_2048 with PSS
JWKS_ALG = getattr(settings, "SV_JWKS_ALG", "ES256")


def get_jwks_keys() -> list[dict]:
    """
    Get all public keys to publish in JWKS.

    During migration, this will include both AWS and Google Cloud KMS keys.
    After migration, only Google Cloud KMS keys.

    Returns:
        List of JWK dictionaries
    """
    keys = []

    # Google Cloud KMS keys (preferred)
    gcp_keys = getattr(settings, "GCP_KMS_JWKS_KEYS", [])
    if gcp_keys:
        from validibot.core.gcp_kms import jwk_from_gcp_key

        for key_name in gcp_keys:
            try:
                jwk = jwk_from_gcp_key(key_name, JWKS_ALG)
                keys.append(jwk)
            except Exception:
                # Log error but don't fail entire JWKS
                import logging

                logging.exception(f"Failed to load GCP KMS key: {key_name}")

    # AWS KMS keys (legacy, during migration only)
    aws_keys = getattr(settings, "AWS_KMS_JWKS_KEYS", [])
    if aws_keys:
        try:
            import boto3
            from validibot.core.aws_kms import jwk_from_kms_key

            for key_id in aws_keys:
                try:
                    jwk = jwk_from_kms_key(key_id, JWKS_ALG)
                    keys.append(jwk)
                except Exception:
                    import logging

                    logging.exception(f"Failed to load AWS KMS key: {key_id}")
        except ImportError:
            # boto3 not installed (post-migration)
            pass

    return keys
```

#### Step 2.4: Rename and Preserve AWS KMS Code

**File**: `validibot/core/aws_kms.py` (RENAME from `jwks.py`)

Move the existing AWS KMS code to a separate module for the transition period:

```bash
# Rename existing jwks.py AWS code to aws_kms.py
# (Keep only the AWS-specific functions, remove JWKS endpoint logic)
```

```python
# core/aws_kms.py
"""AWS KMS integration (LEGACY - for migration period only)."""

import base64
from functools import lru_cache

import boto3
from authlib.jose import JsonWebKey
from cryptography.hazmat.primitives import serialization
from django.conf import settings


def _kms():
    return boto3.client(
        "kms", region_name=getattr(settings, "AWS_DEFAULT_REGION", None)
    )


@lru_cache(maxsize=64)
def get_public_key_der(key_id_or_alias: str) -> bytes:
    resp = _kms().get_public_key(KeyId=key_id_or_alias)
    return resp["PublicKey"]  # ASN.1 DER


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def kid_from_der(der: bytes) -> str:
    # Simple, stable kid: SHA-256 thumbprint of DER
    import hashlib

    return _b64url(hashlib.sha256(der).digest())


def jwk_from_kms_key(key_id_or_alias: str, alg: str):
    der = get_public_key_der(key_id_or_alias)
    public_key = serialization.load_der_public_key(der)
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_jwk = JsonWebKey.import_key(pem).as_dict()
    public_jwk["use"] = "sig"
    public_jwk["alg"] = alg
    public_jwk["kid"] = kid_from_der(der)
    return public_jwk
```

#### Step 2.5: Update JWKS View

**File**: `validibot/marketing/views.py` (or wherever JWKS view is)

```python
from django.http import JsonResponse
from validibot.core.jwks import get_jwks_keys


def jwks_view(request):
    """
    JWKS endpoint for publishing public signing keys.

    Returns:
        JSON Web Key Set with all active public keys
    """
    keys = get_jwks_keys()

    return JsonResponse(
        {"keys": keys},
        content_type="application/jwk-set+json",
    )
```

#### Step 2.6: Update Settings

**File**: `config/settings/production.py`

```python
# Google Cloud KMS Configuration
GCP_KMS_SIGNING_KEY = env(
    "GCP_KMS_SIGNING_KEY",
    default="projects/PROJECT_ID/locations/australia-southeast1/keyRings/validibot-keys/cryptoKeys/credential-signing",
)

GCP_KMS_JWKS_KEYS = env.list(
    "GCP_KMS_JWKS_KEYS",
    default=[GCP_KMS_SIGNING_KEY],
)

# AWS KMS Configuration (LEGACY - during migration only)
# After migration complete, these can be removed
AWS_KMS_JWKS_KEYS = env.list("AWS_KMS_JWKS_KEYS", default=[])
AWS_DEFAULT_REGION = env("AWS_DEFAULT_REGION", default="ap-southeast-2")

# JWKS Configuration
SV_JWKS_ALG = env("SV_JWKS_ALG", default="ES256")
```

**File**: `config/settings/local.py`

```python
# For local development, use dev key or skip KMS entirely
GCP_KMS_SIGNING_KEY = env(
    "GCP_KMS_SIGNING_KEY",
    default="",  # Empty = offline development
)

GCP_KMS_JWKS_KEYS = env.list("GCP_KMS_JWKS_KEYS", default=[])

# No AWS keys in local development
AWS_KMS_JWKS_KEYS = []
```

**Deliverables**:

- ✅ New `gcp_kms.py` module created
- ✅ Updated `jwks.py` to support both providers
- ✅ AWS code moved to `aws_kms.py`
- ✅ Settings updated with GCP KMS configuration
- ✅ Dependencies updated

---

### Phase 3: Deployment (Transition Period)

**Timeline**: Day 2

#### Step 3.1: Update Environment Variables

Set these in Google Secret Manager or Cloud Run:

```bash
# Production
GCP_KMS_SIGNING_KEY="projects/PROJECT_ID/locations/australia-southeast1/keyRings/validibot-keys/cryptoKeys/credential-signing"

# Publish BOTH keys during transition
GCP_KMS_JWKS_KEYS="projects/PROJECT_ID/locations/australia-southeast1/keyRings/validibot-keys/cryptoKeys/credential-signing"
AWS_KMS_JWKS_KEYS="alias/sv-credential-signing-prod"

# Keep AWS credentials during transition
AWS_DEFAULT_REGION="ap-southeast-2"
AWS_ACCESS_KEY_ID="..."  # If not using IAM roles
AWS_SECRET_ACCESS_KEY="..."  # If not using IAM roles

SV_JWKS_ALG="ES256"
```

#### Step 3.2: Deploy to Staging

```bash
# Deploy to staging environment first
just gcp-deploy-staging

# Verify JWKS endpoint shows BOTH keys
curl https://staging.validibot.com/.well-known/jwks.json | jq '.keys | length'
# Should return 2 (one AWS, one GCP)

# Test credential signing still works
# (Run integration tests or manual test)
```

#### Step 3.3: Deploy to Production

```bash
# Deploy to production
just gcp-deploy

# Verify JWKS endpoint
curl https://app.validibot.com/.well-known/jwks.json | jq

# Monitor Cloud Monitoring for errors
gcloud run services logs read validibot-web \
  --region australia-southeast1 \
  --limit 50
```

**Deliverables**:

- ✅ Staging deployment successful
- ✅ JWKS publishes both AWS and GCP keys
- ✅ Production deployment successful
- ✅ No credential signing errors

---

### Phase 4: Cutover to Google Cloud KMS

**Timeline**: Day 3

#### Step 4.1: Update Signing Code to Use Google Cloud KMS

**File**: `validibot/validations/services/credential.py` (or wherever signing happens)

Before:

```python
# OLD: Using AWS KMS
import boto3

def sign_credential(claims: dict) -> str:
    # ... build JWT header and payload ...
    kms = boto3.client("kms")
    response = kms.sign(KeyId="alias/sv-credential-signing-prod", ...)
    # ... return signed JWT ...
```

After:

```python
# NEW: Using Google Cloud KMS
from validibot.core.gcp_kms import sign_data
from django.conf import settings

def sign_credential(claims: dict) -> str:
    # ... build JWT header and payload ...
    message = f"{header_b64}.{payload_b64}"
    signature = sign_data(settings.GCP_KMS_SIGNING_KEY, message.encode())
    # ... return signed JWT ...
```

#### Step 4.2: Deploy Cutover

```bash
# Deploy to staging
just gcp-deploy-staging

# Test new credentials are signed with Google Cloud KMS
# Verify kid in JWT header matches GCP key in JWKS

# Deploy to production
just gcp-deploy
```

#### Step 4.3: Monitor

```bash
# Watch for signing errors
gcloud run services logs read validibot-web \
  --region australia-southeast1 \
  --filter "severity>=ERROR" \
  --limit 100

# Check KMS metrics
gcloud monitoring time-series list \
  --filter 'metric.type="cloudkms.googleapis.com/api/request_count"' \
  --interval-start $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --interval-end $(date -u +%Y-%m-%dT%H:%M:%SZ)
```

**Deliverables**:

- ✅ All new credentials signed with Google Cloud KMS
- ✅ AWS KMS no longer used for signing
- ✅ JWKS still publishes both keys (for existing credentials)
- ✅ No errors in production

---

### Phase 5: AWS KMS Sunset (After 90 Days)

**Timeline**: Day 90+

After 90 days (credential expiry period), all AWS-signed credentials will have expired.

#### Step 5.1: Remove AWS Key from JWKS

Update environment variables:

```bash
# Remove AWS key from JWKS
GCP_KMS_JWKS_KEYS="projects/PROJECT_ID/locations/australia-southeast1/keyRings/validibot-keys/cryptoKeys/credential-signing"
AWS_KMS_JWKS_KEYS=""  # Empty

# Can also remove AWS credentials
unset AWS_ACCESS_KEY_ID
unset AWS_SECRET_ACCESS_KEY
unset AWS_DEFAULT_REGION
```

Deploy:

```bash
just gcp-deploy
```

Verify:

```bash
# JWKS should only show GCP key
curl https://app.validibot.com/.well-known/jwks.json | jq '.keys | length'
# Should return 1
```

#### Step 5.2: Remove AWS Code

```bash
# Delete AWS KMS module
rm validibot/core/aws_kms.py

# Update jwks.py to remove AWS support
# (Remove aws_keys logic)

# Remove boto3 dependency
# Edit pyproject.toml, remove boto3
uv lock
uv sync
```

#### Step 5.3: Remove AWS Environment Variables

**File**: `.envs/.production/.django`

```bash
# DELETE these lines:
# SV_SIGN_ALG="ECDSA_SHA_256"
# SV_REGION="ap-southeast-2"
# SV_ALIAS="alias/sv-credential-signing-prod"
# SV_KEY_ARN="arn:aws:kms:us-west-1:437449854773:key/..."
# AWS_DEFAULT_REGION="ap-southeast-2"
```

#### Step 5.4: Update Documentation

- ✅ Remove AWS KMS references from [configure-jwks.md](../dev_docs/how-to/configure-jwks.md)
- ✅ Update with Google Cloud KMS instructions only
- ✅ Archive this ADR as completed

#### Step 5.5: Decommission AWS Resources

```bash
# Schedule AWS KMS key for deletion (30-day waiting period)
aws kms schedule-key-deletion \
  --key-id "arn:aws:kms:us-west-1:437449854773:key/..." \
  --pending-window-in-days 30

# Remove AWS IAM user/role if created specifically for this
```

**Deliverables**:

- ✅ AWS code and dependencies removed
- ✅ JWKS only publishes Google Cloud KMS key
- ✅ Documentation updated
- ✅ AWS resources decommissioned

---

## Testing Plan

### Unit Tests

**File**: `validibot/core/tests/test_gcp_kms.py` (NEW)

```python
import pytest
from unittest.mock import Mock, patch
from validibot.core.gcp_kms import (
    jwk_from_gcp_key,
    sign_data,
    kid_from_der,
)


@pytest.fixture
def mock_kms_client():
    with patch("validibot.core.gcp_kms.kms.KeyManagementServiceClient") as mock:
        yield mock.return_value


def test_get_public_key_der(mock_kms_client):
    """Test fetching public key from GCP KMS."""
    # Mock response
    mock_response = Mock()
    mock_response.pem = b"-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
    mock_kms_client.get_public_key.return_value = mock_response

    from validibot.core.gcp_kms import get_public_key_der

    der = get_public_key_der("projects/test/locations/us/keyRings/test/cryptoKeys/test")

    assert isinstance(der, bytes)
    mock_kms_client.get_public_key.assert_called_once()


def test_jwk_from_gcp_key(mock_kms_client):
    """Test JWK conversion."""
    # Mock valid EC public key
    # ... test implementation ...
    pass


def test_sign_data(mock_kms_client):
    """Test signing with GCP KMS."""
    mock_response = Mock()
    mock_response.signature = b"signature_bytes"
    mock_kms_client.asymmetric_sign.return_value = mock_response

    signature = sign_data("projects/test/locations/us/keyRings/test/cryptoKeys/test", b"data")

    assert signature == b"signature_bytes"
```

### Integration Tests

**File**: `validibot/core/tests/test_jwks_integration.py`

```python
import pytest
from django.test import Client


@pytest.mark.integration
def test_jwks_endpoint_returns_keys(client: Client):
    """Test JWKS endpoint returns valid key set."""
    response = client.get("/.well-known/jwks.json")

    assert response.status_code == 200
    assert response["Content-Type"] == "application/jwk-set+json"

    data = response.json()
    assert "keys" in data
    assert isinstance(data["keys"], list)

    # During transition, should have 2 keys (AWS + GCP)
    # After migration, should have 1 key (GCP only)
    assert len(data["keys"]) >= 1

    # Verify key structure
    for key in data["keys"]:
        assert "kty" in key  # Key type
        assert "use" in key  # Usage (sig)
        assert "alg" in key  # Algorithm (ES256)
        assert "kid" in key  # Key ID
```

### Manual Testing Checklist

- [ ] Staging: JWKS endpoint returns both keys
- [ ] Staging: Can sign credential with GCP KMS
- [ ] Staging: Can verify credential with published JWK
- [ ] Production: JWKS endpoint returns both keys
- [ ] Production: Can sign credential with GCP KMS
- [ ] Production: Can verify credential with published JWK
- [ ] After cutover: New credentials use GCP kid
- [ ] After 90 days: JWKS only shows GCP key
- [ ] After 90 days: No AWS dependencies remain

---

## Rollback Plan

### During Transition (Phases 3-4)

If issues arise:

1. **Revert to AWS KMS signing**: Update signing code to use AWS KMS again
2. **Keep both keys in JWKS**: Don't remove AWS key from JWKS
3. **Debug Google Cloud KMS issues**: Check IAM permissions, network connectivity

### After Cutover (Phase 4+)

If Google Cloud KMS fails:

1. **Emergency**: Temporarily re-enable AWS KMS signing (requires AWS credentials still available)
2. **Fix GCP issues**: Resolve IAM, network, or code issues
3. **Re-cutover**: Switch back to Google Cloud KMS when fixed

**Important**: Keep AWS KMS key and credentials available for 90 days after cutover as emergency fallback.

### After AWS Sunset (Phase 5+)

No rollback possible. If Google Cloud KMS fails:

1. **Create new AWS KMS key** (emergency only)
2. **Deploy emergency signing code** (use archived AWS code)
3. **Publish new JWKS** with emergency key

---

## Security Considerations

### During Migration

- **Dual Key Publication**: Temporarily publishing both AWS and GCP keys increases attack surface slightly, but necessary for transition
- **Credential Lifetime**: 90-day expiry ensures old AWS-signed credentials expire naturally
- **Access Controls**: Both AWS and GCP KMS keys have appropriate IAM restrictions

### Post-Migration

- **Single Provider**: Only Google Cloud KMS reduces complexity
- **Regional Isolation**: Key stored only in `australia-southeast1`
- **Automatic Rotation**: 90-day rotation policy
- **Audit Logging**: All KMS operations logged in Cloud Audit Logs

---

## Cost Analysis

### Current (AWS KMS)

- **Key Storage**: $1/month
- **Operations**: $0.03 per 10,000 operations
- **Estimated Monthly**: ~$1.50/month (assuming 15k operations)

### Future (Google Cloud KMS)

- **Key Storage**: $0.06/month per version (1-2 versions)
- **Operations**: $0.03 per 10,000 operations
- **Estimated Monthly**: ~$0.15/month (assuming 15k operations)

**Savings**: ~$1.35/month (~90% reduction)

---

## Monitoring and Alerts

### Key Metrics to Monitor

1. **KMS Request Success Rate**

   ```
   cloudkms.googleapis.com/api/request_count
   (filter: response_code < 400)
   ```

2. **KMS Request Latency**

   ```
   cloudkms.googleapis.com/api/request_latencies
   ```

3. **JWKS Endpoint Availability**

   ```
   Cloud Monitoring uptime check on /.well-known/jwks.json
   ```

4. **Credential Signing Errors**
   ```
   Log-based metric on "Failed to sign credential"
   ```

### Recommended Alerts

- **KMS Failure Rate > 5%**: Page on-call
- **JWKS Endpoint Down**: Page on-call
- **KMS Request Latency > 2s**: Warning
- **Unauthorized KMS Access Attempts**: Security alert

---

## Timeline Summary

| Phase                          | Duration | Status      |
| ------------------------------ | -------- | ----------- |
| Phase 1: Infrastructure Setup  | Day 1    | Not Started |
| Phase 2: Code Migration        | Days 1-2 | Not Started |
| Phase 3: Transition Deployment | Day 2    | Not Started |
| Phase 4: Cutover to GCP        | Day 3    | Not Started |
| Phase 5: AWS Sunset            | Day 90+  | Not Started |

**Total Migration Time**: 3 days active work + 90-day transition period

---

## Success Criteria

Migration is considered successful when:

- ✅ Google Cloud KMS key created and accessible
- ✅ JWKS endpoint publishes Google Cloud KMS public key
- ✅ New credentials signed with Google Cloud KMS
- ✅ Old credentials verified with AWS key (during transition)
- ✅ No signing errors in production
- ✅ After 90 days: AWS KMS completely removed
- ✅ Cost reduced by 90%

---

## References

- [Google Cloud KMS Documentation](../dev_docs/google_cloud/kms.md)
- [ADR: Heroku to GCP Migration](2025-12-02-heroku-to-gcp-migration.md)
- [Google Cloud KMS API Reference](https://cloud.google.com/kms/docs/reference/rest)
- [JWT Best Practices RFC 8725](https://datatracker.ietf.org/doc/html/rfc8725)
