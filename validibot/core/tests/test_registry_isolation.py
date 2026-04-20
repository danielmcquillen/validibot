"""
Tests that prove the autouse license-isolation fixture works.

Covers refactor-step item ``[review-#10]`` from
`validibot-project/docs/adr/2026-04-18-architecture-refactor-step.md`.

### What we're guarding against

The active :class:`~validibot.core.license.License` is held in a
module-level variable (``validibot.core.license._current_license``)
that commercial packages overwrite at import time via
:func:`set_license`. If one test calls ``set_license(...)`` and
doesn't restore it, every subsequent test in the same pytest
worker process sees the leaked state. Symptoms are
non-deterministic failures that depend on test-ordering — the
worst kind to debug.

The fix is a single autouse fixture in the root ``conftest.py``
that snapshots the license at test start and restores at teardown.
Because features now travel on the License object, one snapshot
covers both the tier (``edition``) and the per-feature gating
(``features``) — there's no separate feature registry to track.

The tests below prove the fixture works without needing to
manually reset state at teardown.

### Why these tests are baseline-agnostic

The autouse fixture's contract is "restore to whatever baseline
existed at test start," NOT "restore to community / empty." That
distinction matters because Pro/Enterprise packages
``set_license(...)`` at import time — the baseline in a
Pro-installed test environment is a Pro license, not a Community
one, and hard-coding either would make the test
environment-dependent.

Each pair:

1. Captures the baseline License at the START of the first test
   (stashed on the class).
2. Constructs a polluting License that differs from baseline in a
   demonstrable way.
3. Uses that polluter in test_a (calls ``set_license``, no
   cleanup).
4. Asserts in test_b that ``get_license()`` returns the baseline
   — regardless of what that baseline contains.

If the autouse fixture regresses, test_b fails.
"""

from __future__ import annotations

from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import get_license
from validibot.core.license import set_license


class TestLicenseIsolation:
    """Two-test pair proving License overrides don't leak.

    Baseline-agnostic: works whether the test environment has
    commercial packages installed or not. If the autouse
    ``_snapshot_license`` fixture is removed, the second test
    fails because the first test's polluting license persists.
    """

    _baseline: License | None = None

    def test_a_polluter_overrides_license_without_cleanup(self):
        """Capture the baseline License, construct a polluter that
        is guaranteed to differ from it, and
        ``set_license(polluter)`` without cleanup.

        The autouse fixture's teardown is the only thing that can
        undo this — this test intentionally does NOT manually
        restore.
        """
        cls = TestLicenseIsolation
        cls._baseline = get_license()

        # Build a polluter that differs from baseline on BOTH
        # axes (edition and features) so test_b's assertion can
        # catch either class of leak.
        polluter_edition = (
            Edition.COMMUNITY
            if cls._baseline.edition != Edition.COMMUNITY
            else Edition.ENTERPRISE
        )
        polluter = License(
            edition=polluter_edition,
            features=frozenset(
                {
                    # Some feature that is almost certainly NOT in
                    # the baseline's feature set regardless of
                    # which commercial package might be installed.
                    # Using SAML_SSO is safe: only Enterprise
                    # offers it, so any non-Enterprise baseline
                    # will definitely differ here.
                    CommercialFeature.SAML_SSO.value,
                    "synthetic_polluter_feature",
                },
            ),
        )

        # Sanity: the polluter really differs from baseline.
        assert polluter != cls._baseline

        set_license(polluter)
        assert get_license() == polluter

    def test_b_license_returns_to_captured_baseline(self):
        """The polluter from the prior test must be gone; the
        License must exactly match the baseline captured at test_a
        entry.

        Equality is compared on the frozen dataclass — if either
        the edition or the features frozenset has drifted, the
        assertion fails with a message showing both baselines.
        """
        cls = TestLicenseIsolation
        assert cls._baseline is not None, (
            "test_a must run first — pytest ran tests out of order, "
            "or the polluter test was skipped"
        )

        current = get_license()
        assert current == cls._baseline, (
            f"license did not restore to baseline: expected "
            f"{cls._baseline}, got {current}; the "
            "``_snapshot_license`` autouse fixture in conftest.py "
            "did not run or did not restore"
        )
