"""
Root-level pytest configuration for Validibot tests.

Autouse fixtures and reporting hooks defined here apply to every test
discovered by pytest from this project root — anything under ``tests/``
or ``validibot/``. This file is intentionally minimal: the existing
``tests/conftest.py`` and ``validibot/conftest.py`` hold domain-specific
fixtures; this root-level file only holds process-scope state resets and
global test-run reporting.

### What this file is responsible for

Resetting the module-level license singleton in
:mod:`validibot.core.license` around each test. Commercial packages
call ``set_license(PRO_LICENSE)`` at import time; a test that
overrides the license via ``set_license(...)`` must not leak that
override to the next test in the same worker process. Symptom when
this goes wrong: non-deterministic test failures that depend on
test ordering, or tests that pass in isolation but fail as part of
the full suite.

The fix snapshots the license at test start and restores at
teardown, so every test sees the baseline it was designed against.
Features travel on the License object (see
:mod:`validibot.core.features`), so one snapshot covers both the
edition and the feature set — no separate feature-registry
snapshot is needed.

See refactor-step item ``[review-#10]`` in
``validibot-project/docs/adr/2026-04-18-architecture-refactor-step.md``.
"""

from __future__ import annotations

import os

import pytest


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Explain env-gated optional suites before pytest starts running tests."""

    notices = []
    if not os.environ.get("SMOKE_TEST_STAGE"):
        notices.append(
            "smoke deployment tests will skip: SMOKE_TEST_STAGE is not set",
        )
    elif not os.environ.get("GCP_PROJECT_ID"):
        notices.append(
            "smoke deployment tests will skip: GCP_PROJECT_ID is not set",
        )
    elif not (
        os.environ.get("SUPERUSER_USERNAME") and os.environ.get("SUPERUSER_PASSWORD")
    ):
        notices.append(
            "authenticated smoke tests will skip: "
            "SUPERUSER_USERNAME/SUPERUSER_PASSWORD are not set",
        )

    missing_fullstack = [
        name
        for name in (
            "FULLSTACK_API_TOKEN",
            "FULLSTACK_ORG_SLUG",
            "FULLSTACK_WORKFLOW_ID",
        )
        if not os.environ.get(name)
    ]
    if missing_fullstack:
        notices.append(
            "full-stack E2E tests will skip if selected: missing "
            + ", ".join(missing_fullstack),
        )

    selected_args = " ".join(config.args).lower()
    if "mcp" in selected_args and not os.environ.get("VALIDIBOT_E2E_TOKEN"):
        notices.append(
            "MCP live E2E tests will skip if selected: VALIDIBOT_E2E_TOKEN is not set",
        )

    if not notices:
        return []
    return ["Env-gated optional tests:", *[f"  - {notice}" for notice in notices]]


@pytest.fixture(autouse=True)
def _snapshot_license():
    """Snapshot the current :class:`License` and restore after each test.

    The baseline is whatever import-time commercial-package
    registrations produced — e.g. in a Pro-installed test
    environment, Pro's ``set_license(PRO_LICENSE)`` has already
    run, so the baseline is the Pro license. Tests that override
    the license mid-body get reverted at teardown; cross-test
    isolation is guaranteed either way.

    The License dataclass is frozen, so snapshot+restore is just
    re-binding the module-level reference. No defensive copy
    needed.
    """
    from validibot.core.license import get_license
    from validibot.core.license import set_license

    baseline = get_license()
    try:
        yield
    finally:
        set_license(baseline)
