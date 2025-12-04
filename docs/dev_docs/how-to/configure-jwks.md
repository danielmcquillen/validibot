# Configure the Badge JWKS Endpoint

Validibot advertises badge-signing public keys at `/.well-known/jwks.json`. Badge verifiers fetch this document to validate the signatures we apply with Google Cloud KMS.

## Production checklist

- Provision an asymmetric KMS key (see [docs/dev_docs/google_cloud/kms.md](../google_cloud/kms.md)) and grant the web application **GetPublicKey** access.
- Set the following environment variables:
  - `GCP_KMS_SIGNING_KEY` – the full resource name of the active key used for signing badges (e.g., `projects/PROJECT_ID/locations/australia-southeast1/keyRings/validibot-keys/cryptoKeys/credential-signing`).
  - `GCP_KMS_JWKS_KEYS` – comma-separated list of key resource names that should be published. During rotation include both the new and previous key until old badges expire. Defaults to `[GCP_KMS_SIGNING_KEY]`.
  - `SV_JWKS_ALG` – advertised signing algorithm (`ES256` for the current ECC key).
- Deploy and confirm that `/.well-known/jwks.json` returns the expected key set and serves with the `application/jwk-set+json` content type.

## Local development options

1. **Use the real KMS key** – if you have Google Cloud credentials with `cloudkms.cryptoKeyVersions.viewPublicKey` permission, ensure `gcloud auth application-default login` is configured, and set `GCP_KMS_JWKS_KEYS` to the key resource name. The endpoint will call Google Cloud KMS directly and return the live key.
2. **Work offline** – set `GCP_KMS_JWKS_KEYS=` (empty string) in your local environment. The endpoint will return an empty key set so you can develop without calling Google Cloud. Tests cover the successful path via mocks.

The helper `simplevalidations.core.gcp_kms.jwk_from_gcp_key` converts the KMS public key response into a JSON Web Key using `authlib`. When updating the signing infrastructure, keep the conversion logic in sync with the chosen key type and extend the tests in [simplevalidations/core/tests/test_gcp_kms.py](../../simplevalidations/core/tests/test_gcp_kms.py).
