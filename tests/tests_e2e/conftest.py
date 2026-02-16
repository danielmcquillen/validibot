"""
Pytest fixtures for E2E stress tests.

These tests run against a live Validibot environment (Docker Compose or deployed).
When run via ``just test-e2e``, environment variables are auto-provisioned
by the ``setup_fullstack_test_data`` management command. They can also be set
manually:

- FULLSTACK_API_URL: Base API URL (default: http://localhost:8000/api/v1)
- FULLSTACK_API_TOKEN: Valid API bearer token
- FULLSTACK_ORG_SLUG: Organization slug
- FULLSTACK_WORKFLOW_ID: Workflow UUID with at least one validation step

See docs/dev_docs/how-to/run-e2e-tests.md for details.
"""

from __future__ import annotations

import os

import httpx
import pytest


def _require_env(name: str) -> str:
    """Get a required environment variable or skip the test."""
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(
            f"Missing {name}. Run via 'just test-e2e' for auto-provisioning.",
        )
    return value


@pytest.fixture(scope="session")
def api_url() -> str:
    """Base API URL for the running Validibot instance."""
    return os.environ.get("FULLSTACK_API_URL", "http://localhost:8000/api/v1")


@pytest.fixture(scope="session")
def api_token() -> str:
    """API bearer token for authentication."""
    return _require_env("FULLSTACK_API_TOKEN")


@pytest.fixture(scope="session")
def org_slug() -> str:
    """Organization slug for API routes."""
    return _require_env("FULLSTACK_ORG_SLUG")


@pytest.fixture(scope="session")
def workflow_id() -> str:
    """Workflow UUID to run validations against."""
    return _require_env("FULLSTACK_WORKFLOW_ID")


@pytest.fixture(scope="session")
def http_client(api_url: str, api_token: str) -> httpx.Client:
    """
    httpx client configured for the running Validibot instance.

    Session-scoped so connection pooling is shared across tests.
    Not used for concurrent submissions (each thread creates its own client),
    but useful for setup/teardown verification.
    """
    client = httpx.Client(
        base_url=api_url,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
        },
        timeout=60.0,
    )
    yield client
    client.close()
