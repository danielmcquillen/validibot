"""
Reusable helpers for E2E workflow tests.

These helpers provide a standard submit-poll-assert pattern for testing
complete validation workflows against a running Validibot instance.  Each
E2E test file defines its scenario (payload, expected outcome) and uses
these helpers for the HTTP plumbing and common assertions.

Design rationale: by centralising the HTTP client logic and assertion
helpers here, adding a new E2E workflow test (e.g. FMU, another
EnergyPlus scenario) requires only a test file with scenario definitions
— no duplicated polling or response-parsing code.

Usage::

    from tests.tests_e2e.helpers import submit_and_poll, assert_run_passed

    result = submit_and_poll(api_url, api_token, org_slug, workflow_id, payload)
    assert_run_passed(result)
"""

from __future__ import annotations

import logging
import time
from http import HTTPStatus

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Polling configuration
# ---------------------------------------------------------------------------
# Advanced validators (EnergyPlus, FMU) run real simulations in Docker
# containers, which typically take 1-5 minutes.  The default timeout is
# generous to handle cold-start and CI environments.

DEFAULT_POLL_TIMEOUT_S = 420.0  # 7 minutes
DEFAULT_POLL_INTERVAL_S = 5.0

TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "TIMED_OUT"})


# ---------------------------------------------------------------------------
# Core HTTP helpers
# ---------------------------------------------------------------------------


def poll_until_complete(
    client: httpx.Client,
    url: str,
    *,
    timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> tuple[dict, int]:
    """Poll a validation run endpoint until it reaches a terminal status.

    Returns the final response body and HTTP status code.  If the timeout
    is reached before a terminal status, returns the last response seen.
    """
    deadline = time.time() + timeout_s
    start = time.time()
    last: dict = {}
    last_status_code = 0
    poll_count = 0

    while time.time() < deadline:
        resp = client.get(url)
        last_status_code = resp.status_code
        poll_count += 1
        if resp.status_code == HTTPStatus.OK:
            data = resp.json()
            last = data
            status = (data.get("status") or "").upper()

            if status in TERMINAL_STATUSES:
                elapsed = time.time() - start
                result_str = (data.get("result") or "").upper()
                logger.info(
                    "Run %s completed: status=%s result=%s (%.0fs, %d polls)",
                    str(data.get("id", "?"))[:8],
                    status,
                    result_str,
                    elapsed,
                    poll_count,
                )
                return data, resp.status_code

            remaining = deadline - time.time()
            elapsed = time.time() - start
            logger.info(
                "  Waiting... status=%s (%.0fs elapsed, %.0fs remaining)",
                status,
                elapsed,
                remaining,
            )
        time.sleep(interval_s)

    elapsed = time.time() - start
    logger.warning(
        "Polling timed out after %.0fs (%d polls). Last status: %s",
        elapsed,
        poll_count,
        (last.get("status") or "unknown"),
    )
    return last, last_status_code


def submit_and_poll(
    api_url: str,
    api_token: str,
    org_slug: str,
    workflow_id: str,
    payload: dict,
    *,
    timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> dict:
    """Submit a validation run via the API and poll until completion.

    Creates its own ``httpx.Client`` for thread safety and connection
    isolation.

    Returns a dict with keys:
        - ``run_id``: UUID string (or None on submission failure)
        - ``status``: terminal status string
        - ``result``: PASS/FAIL/ERROR/... result string
        - ``data``: full API response body
        - ``error``: error message string (or None on success)
    """
    with httpx.Client(
        headers={
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
        },
        timeout=60.0,
    ) as client:
        start_url = f"{api_url}/orgs/{org_slug}/workflows/{workflow_id}/runs/"

        # Log the payload so the user can see what parameters are being tested
        param_summary = ", ".join(f"{k}={v}" for k, v in sorted(payload.items()))
        logger.info("Submitting run: %s", param_summary)
        logger.info("  POST %s", start_url)
        resp = client.post(start_url, json=payload)

        if resp.status_code not in (
            HTTPStatus.OK,
            HTTPStatus.CREATED,
            HTTPStatus.ACCEPTED,
        ):
            logger.error(
                "Submission failed: HTTP %d — %s",
                resp.status_code,
                resp.text[:500],
            )
            return {
                "error": f"Submission failed: {resp.status_code} {resp.text[:500]}",
                "run_id": None,
                "status": None,
                "result": None,
                "data": {},
            }

        data = resp.json()
        run_id = data.get("id")
        logger.info("  Run created: %s — polling for completion...", run_id)

        poll_url = f"{api_url}/orgs/{org_slug}/runs/{run_id}/"
        result_data, _status_code = poll_until_complete(
            client,
            poll_url,
            timeout_s=timeout_s,
            interval_s=poll_interval_s,
        )

        return {
            "run_id": result_data.get("id"),
            "status": (result_data.get("status") or "").upper(),
            "result": (result_data.get("result") or "").upper(),
            "data": result_data,
            "error": None,
        }


# ---------------------------------------------------------------------------
# Response data extraction helpers
# ---------------------------------------------------------------------------


def get_output_signals(result: dict) -> dict[str, float | str | None]:
    """Extract output signals from the first step as a slug→value mapping.

    Returns an empty dict if no steps or signals are present.
    """
    steps = result.get("data", {}).get("steps", [])
    if not steps:
        return {}

    signals = {}
    for sig in steps[0].get("output_signals", []):
        slug = sig.get("slug", "")
        value = sig.get("value")
        if slug:
            signals[slug] = value
    return signals


def get_output_signal(result: dict, slug: str) -> float | None:
    """Extract a single output signal value by slug.

    Returns None if the signal is not found.
    """
    signals = get_output_signals(result)
    return signals.get(slug)


def get_step_issues(
    result: dict,
    *,
    severity: str = "ERROR",
) -> list[dict]:
    """Get issues of a given severity from the first step.

    Returns an empty list if no matching issues are found.
    """
    steps = result.get("data", {}).get("steps", [])
    if not steps:
        return []

    return [
        issue
        for issue in steps[0].get("issues", [])
        if (issue.get("severity") or "").upper() == severity.upper()
    ]


def get_template_parameters_used(result: dict) -> dict:
    """Extract template_parameters_used from the first step.

    Returns an empty dict if no template parameters metadata is present.
    """
    steps = result.get("data", {}).get("steps", [])
    if not steps:
        return {}

    return steps[0].get("template_parameters_used", {})


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def assert_submission_succeeded(result: dict) -> None:
    """Assert that the API submission itself did not fail.

    This checks the HTTP-level submission, not the validation outcome.
    """
    assert result["error"] is None, f"Submission failed: {result['error']}"
    assert result["run_id"] is not None, "No run_id in response"


def assert_run_passed(result: dict) -> None:
    """Assert the run completed successfully with all assertions passing.

    Checks: status=SUCCEEDED, result=PASS, no ERROR-severity issues.
    """
    assert_submission_succeeded(result)

    assert result["status"] == "SUCCEEDED", (
        f"Expected SUCCEEDED but got {result['status']}. Data: {result['data']}"
    )
    assert result["result"] == "PASS", (
        f"Expected result=PASS but got {result['result']}. "
        f"Issues: {get_step_issues(result, severity='ERROR')}"
    )

    errors = get_step_issues(result, severity="ERROR")
    assert not errors, (
        f"Expected no ERROR issues but found {len(errors)}: "
        f"{[e.get('message', '') for e in errors]}"
    )


def assert_run_failed_assertion(result: dict) -> None:
    """Assert the simulation ran but an output assertion failed.

    The run status is SUCCEEDED (simulation completed), but the result
    is FAIL (one or more assertions did not pass).
    """
    assert_submission_succeeded(result)

    assert result["status"] == "SUCCEEDED", (
        f"Expected SUCCEEDED (simulation ran) but got {result['status']}. "
        f"Data: {result['data']}"
    )
    assert result["result"] == "FAIL", (
        f"Expected result=FAIL (assertion failed) but got {result['result']}"
    )

    errors = get_step_issues(result, severity="ERROR")
    assert errors, "Expected at least one ERROR issue from the failed assertion"


def assert_run_failed_preprocessing(result: dict) -> None:
    """Assert the run failed during preprocessing (before simulation).

    This is what happens when input parameters are invalid (out of range,
    missing required, wrong type, etc.).
    """
    assert_submission_succeeded(result)

    assert result["status"] in ("SUCCEEDED", "FAILED"), (
        f"Expected SUCCEEDED or FAILED but got {result['status']}. "
        f"Data: {result['data']}"
    )
    assert result["result"] in ("FAIL", "ERROR"), (
        f"Expected result=FAIL or ERROR but got {result['result']}"
    )
