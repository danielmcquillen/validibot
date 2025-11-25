# Configure the Badge JWKS Endpoint

SimpleValidations now advertises badge-signing public keys at `/.well-known/jwks.json`. Badge verifiers fetch this document to validate the signatures we apply with AWS KMS.

## Production checklist

- Provision an asymmetric KMS key (currently `alias/sv-badge-signing-prod`) and grant the web application **GetPublicKey** access.
- Set the following environment variables:
  - `KMS_KEY_ID` – the active key alias or ARN used for signing badges.
  - `AWS_DEFAULT_REGION` – the region that hosts the key (for example `ap-southeast-2`).
  - `SV_JWKS_KEYS` – comma-separated list of key aliases/ARNs that should be published. During rotation include both the new and previous key until old badges expire.
  - `SV_JWKS_ALG` – advertised signing algorithm (`ES256` for the current ECC key).
- Deploy and confirm that `/.well-known/jwks.json` returns the expected key set and serves with the `application/jwk-set+json` content type.

## Local development options

1. **Use the real KMS key** – if you have AWS credentials with `kms:GetPublicKey`, export `AWS_PROFILE`/`AWS_ACCESS_KEY_ID` etc., and set `SV_JWKS_KEYS` to the alias you want to exercise. The endpoint will hit AWS directly and return the live key.
2. **Work offline** – set `SV_JWKS_KEYS=` (empty string) in your local environment. The endpoint will return an empty key set so you can develop without calling AWS. Tests cover the successful path via mocks.

The helper `simplevalidations.core.jwks.jwk_from_kms_key` converts the KMS ASN.1 DER response into a JSON Web Key using `authlib`. When updating the signing infrastructure, keep the conversion logic in sync with the chosen key type and extend the tests in `simplevalidations/core/tests/test_jwks.py`.
