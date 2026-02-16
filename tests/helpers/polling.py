"""Shared polling and URL helpers for validation API tests."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from django.urls import reverse
from rest_framework.status import HTTP_200_OK

TERMINAL_STATUSES = {"SUCCESS", "SUCCEEDED", "FAILED", "COMPLETED", "ERROR"}


def start_workflow_url(workflow: Any) -> str:
    """Resolve the workflow start endpoint."""
    try:
        return reverse(
            "api:org-workflows-runs",
            kwargs={"org_slug": workflow.org.slug, "pk": workflow.pk},
        )
    except Exception:
        return f"/api/v1/orgs/{workflow.org.slug}/workflows/{workflow.pk}/runs/"


def normalize_poll_url(location: str) -> str:
    """Normalize the polling URL returned by a start response."""
    if not location:
        return ""
    if location.startswith("http"):
        parsed = urlparse(location)
        return parsed.path
    return location


def poll_until_complete(
    client: Any,
    url: str,
    timeout_s: float = 30.0,
    interval_s: float = 0.5,
) -> tuple[dict, int]:
    """
    Poll the ValidationRun detail endpoint until a terminal state is reached.

    Works with both DRF APIClient and httpx.Client since both expose
    response.status_code and response.json() with the same interface.

    Returns (json_data, status_code_of_last_poll).
    """
    deadline = time.time() + timeout_s
    last: dict | None = None
    last_status: int | None = None

    while time.time() < deadline:
        resp = client.get(url)
        last_status = resp.status_code
        if resp.status_code == HTTP_200_OK:
            try:
                data = resp.json()
            except Exception:
                data = {}
            last = data
            status = (data.get("status") or data.get("state") or "").upper()
            if status in TERMINAL_STATUSES:
                return data, resp.status_code
        time.sleep(interval_s)

    return last or {}, last_status or 0


def extract_issues(data: dict) -> list[dict]:
    """Collect issues from each validation step in the run payload."""
    steps = data.get("steps") or []
    collected: list[dict] = []
    for step in steps:
        issues = step.get("issues") or []
        if isinstance(issues, list):
            for issue in issues:
                if isinstance(issue, dict):
                    collected.append(issue)
                else:
                    collected.append({"message": str(issue)})
    return collected
