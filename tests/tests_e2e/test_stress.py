"""
E2E concurrent stress tests.

These tests submit multiple validation runs concurrently against a running
Validibot instance (typically started via ``just up``) and verify that:

1. All runs reach a terminal status (no runs lost)
2. No runs are duplicated
3. Each run has a unique ID
4. Results are correct and consistent under concurrent load

Unlike the in-process tests in tests_use_cases/, these exercise the full
production-like stack: HTTP layer, Celery dispatch, Redis broker, and worker
concurrency.

Prerequisites:

- A running Validibot environment (e.g., ``just up``)
- Environment variables configured (see conftest.py)

Running::

    just test-e2e
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from http import HTTPStatus

import httpx

logger = logging.getLogger(__name__)

# How many concurrent runs to submit
CONCURRENT_RUNS = 10

# Polling limits (generous for full-stack with Celery workers)
POLL_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 2.0

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELED", "TIMED_OUT"}


def _poll_until_complete(
    client: httpx.Client,
    url: str,
    timeout_s: float = POLL_TIMEOUT_S,
    interval_s: float = POLL_INTERVAL_S,
) -> tuple[dict, int]:
    """Poll a validation run endpoint until a terminal status or timeout."""
    deadline = time.time() + timeout_s
    last: dict = {}
    last_status = 0

    while time.time() < deadline:
        resp = client.get(url)
        last_status = resp.status_code
        if resp.status_code == HTTPStatus.OK:
            data = resp.json()
            last = data
            status = (data.get("status") or "").upper()
            if status in TERMINAL_STATUSES:
                return data, resp.status_code
        time.sleep(interval_s)

    return last, last_status


def _submit_and_poll(
    api_url: str,
    api_token: str,
    org_slug: str,
    workflow_id: str,
    payload: dict,
) -> dict:
    """
    Submit a validation run and poll until complete.

    Each call creates its own httpx.Client to ensure thread safety.
    """
    with httpx.Client(
        headers={
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
        },
        timeout=60.0,
    ) as client:
        start_url = f"{api_url}/orgs/{org_slug}/workflows/{workflow_id}/runs/"

        resp = client.post(start_url, json=payload)

        if resp.status_code not in (
            HTTPStatus.OK,
            HTTPStatus.CREATED,
            HTTPStatus.ACCEPTED,
        ):
            return {
                "error": f"Start failed: {resp.status_code} {resp.text[:500]}",
                "run_id": None,
                "status": None,
                "data": {},
            }

        data = resp.json()
        run_id = data.get("id")
        poll_url = f"{api_url}/orgs/{org_slug}/runs/{run_id}/"

        result_data, _status_code = _poll_until_complete(client, poll_url)

        return {
            "run_id": result_data.get("id"),
            "status": (result_data.get("status") or "").upper(),
            "data": result_data,
            "error": None,
        }


def _sample_payload() -> dict:
    """A simple JSON payload for stress testing."""
    return {
        "sku": "STRESS-TEST",
        "name": "Stress Test Widget",
        "price": 19.99,
        "rating": 95,
        "tags": ["stress"],
        "dimensions": {"width": 3.5, "height": 1.2},
        "in_stock": True,
    }


class TestConcurrentStress:
    """
    Submit many validation runs concurrently against a live Validibot
    instance and verify all complete correctly.
    """

    def test_concurrent_runs_all_complete(
        self,
        api_url,
        api_token,
        org_slug,
        workflow_id,
    ):
        """All concurrent runs should reach a terminal status."""
        with ThreadPoolExecutor(max_workers=CONCURRENT_RUNS) as executor:
            futures = [
                executor.submit(
                    _submit_and_poll,
                    api_url,
                    api_token,
                    org_slug,
                    workflow_id,
                    _sample_payload(),
                )
                for _ in range(CONCURRENT_RUNS)
            ]
            results = [f.result() for f in as_completed(futures)]

        # Check for submission errors
        errors = [r for r in results if r["error"]]
        assert not errors, f"Some runs failed to start: {errors}"

        # All should have reached a terminal status
        for r in results:
            assert r["status"] in TERMINAL_STATUSES, (
                f"Run {r['run_id']} did not complete: status={r['status']}"
            )

    def test_no_runs_lost_or_duplicated(
        self,
        api_url,
        api_token,
        org_slug,
        workflow_id,
    ):
        """Every submitted run should be trackable with a unique ID."""
        with ThreadPoolExecutor(max_workers=CONCURRENT_RUNS) as executor:
            futures = [
                executor.submit(
                    _submit_and_poll,
                    api_url,
                    api_token,
                    org_slug,
                    workflow_id,
                    _sample_payload(),
                )
                for _ in range(CONCURRENT_RUNS)
            ]
            results = [f.result() for f in as_completed(futures)]

        errors = [r for r in results if r["error"]]
        assert not errors, f"Some runs failed to start: {errors}"

        run_ids = [r["run_id"] for r in results]

        # No None IDs
        assert all(run_ids), f"Some runs have no ID: {run_ids}"

        # All unique
        assert len(set(run_ids)) == CONCURRENT_RUNS, (
            f"Expected {CONCURRENT_RUNS} unique IDs, got {len(set(run_ids))}: {run_ids}"
        )

    def test_results_consistent_under_load(
        self,
        api_url,
        api_token,
        org_slug,
        workflow_id,
    ):
        """All runs with the same payload should produce the same result."""
        with ThreadPoolExecutor(max_workers=CONCURRENT_RUNS) as executor:
            futures = [
                executor.submit(
                    _submit_and_poll,
                    api_url,
                    api_token,
                    org_slug,
                    workflow_id,
                    _sample_payload(),
                )
                for _ in range(CONCURRENT_RUNS)
            ]
            results = [f.result() for f in as_completed(futures)]

        errors = [r for r in results if r["error"]]
        assert not errors, f"Some runs failed to start: {errors}"

        # All runs with identical payloads should have the same status
        statuses = {r["status"] for r in results}
        assert len(statuses) == 1, (
            f"Expected consistent results but got mixed statuses: {statuses}"
        )
