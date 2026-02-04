"""
Post-Deployment Verification (PDV) smoke tests.

These tests run against a deployed environment (dev, staging, or prod) to verify
that critical functionality is working correctly after deployment.

Usage:
    just verify-deployment dev
    just verify-deployment prod

Or directly with pytest:
    SMOKE_TEST_STAGE=dev pytest tests/smoke/ -v

These tests are NOT run as part of the regular test suite. They require:
1. A deployed environment (web + worker services on Cloud Run)
2. Valid GCP credentials to resolve service URLs
3. The SMOKE_TEST_STAGE environment variable set to dev/staging/prod

What gets tested:
- Web service health and accessibility
- Worker service IAM protection (unauthenticated requests should be rejected)
- Callback endpoint security
- API endpoint availability
"""
