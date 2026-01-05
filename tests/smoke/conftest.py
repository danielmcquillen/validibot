"""
Pytest configuration for smoke tests.

Provides fixtures for accessing deployed service URLs and making HTTP requests.
"""

from __future__ import annotations

import os
import subprocess

import pytest
import requests


def _get_service_url(service_name: str, region: str, project: str) -> str:
    """Get the URL of a Cloud Run service using gcloud."""
    # gcloud is a trusted executable with controlled arguments
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "gcloud",
            "run",
            "services",
            "describe",
            service_name,
            f"--region={region}",
            f"--project={project}",
            "--format=value(status.url)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture(scope="session")
def stage() -> str:
    """
    Get the deployment stage to test against.

    Set via SMOKE_TEST_STAGE environment variable.
    """
    stage = os.environ.get("SMOKE_TEST_STAGE", "").lower()
    if stage not in ("dev", "staging", "prod"):
        pytest.skip(
            "SMOKE_TEST_STAGE must be set to 'dev', 'staging', or 'prod'. "
            "Run with: SMOKE_TEST_STAGE=dev pytest tests/smoke/ -v",
        )
    return stage


@pytest.fixture(scope="session")
def gcp_config() -> dict:
    """GCP project and region configuration."""
    return {
        "project": "project-a509c806-3e21-4fbc-b19",
        "region": "australia-southeast1",
    }


@pytest.fixture(scope="session")
def web_service_name(stage: str) -> str:
    """Get the web service name for the given stage."""
    if stage == "prod":
        return "validibot-web"
    return f"validibot-web-{stage}"


@pytest.fixture(scope="session")
def worker_service_name(stage: str) -> str:
    """Get the worker service name for the given stage."""
    if stage == "prod":
        return "validibot-worker"
    return f"validibot-worker-{stage}"


@pytest.fixture(scope="session")
def web_url(web_service_name: str, gcp_config: dict) -> str:
    """
    Get the deployed web service URL.

    This is the public-facing URL for the web service.
    """
    try:
        url = _get_service_url(
            web_service_name,
            gcp_config["region"],
            gcp_config["project"],
        )
    except subprocess.CalledProcessError as e:
        pytest.skip(f"Failed to get web service URL: {e.stderr}")
    else:
        if not url:
            pytest.skip(f"Web service {web_service_name} not deployed")
        return url


@pytest.fixture(scope="session")
def worker_url(worker_service_name: str, gcp_config: dict) -> str:
    """
    Get the deployed worker service URL.

    This is the internal URL for the worker service (IAM-protected).
    """
    try:
        url = _get_service_url(
            worker_service_name,
            gcp_config["region"],
            gcp_config["project"],
        )
    except subprocess.CalledProcessError as e:
        pytest.skip(f"Failed to get worker service URL: {e.stderr}")
    else:
        if not url:
            pytest.skip(f"Worker service {worker_service_name} not deployed")
        return url


@pytest.fixture(scope="session")
def http_session() -> requests.Session:
    """
    Create a requests session for making HTTP calls.

    This session does NOT include any authentication headers.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "validibot-smoke-tests/1.0",
        "Accept": "application/json",
    })
    return session


@pytest.fixture(scope="session")
def authenticated_http_session(gcp_config: dict) -> requests.Session:
    """
    Create a requests session with GCP identity token authentication.

    This session includes an identity token for the current user/service account,
    which can be used to make authenticated requests to IAM-protected services.
    """
    # Get an identity token for the current credentials (gcloud is trusted)
    result = subprocess.run(
        ["gcloud", "auth", "print-identity-token"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    token = result.stdout.strip()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "validibot-smoke-tests/1.0",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    })
    return session
