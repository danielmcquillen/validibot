"""
Root-level pytest configuration for Validibot tests.

Autouse fixtures defined here apply to every test discovered by
pytest from this project root — anything under ``tests/`` or
``validibot/``. This file is intentionally minimal: the existing
``tests/conftest.py`` and ``validibot/conftest.py`` hold
domain-specific fixtures; this root-level file only holds
process-scope state resets that have to run for every single test.

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

import pytest


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
