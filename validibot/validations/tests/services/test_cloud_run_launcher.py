"""
Tests for ``validations/services/cloud_run/launcher.py`` helpers.

This module covers the small pure helpers in the Cloud Run launcher
that don't require a full GCP / Django run context to exercise. The
heavyweight EnergyPlus / FMU launch paths are covered by the
integration suite at ``tests/tests_integration/test_validator_jobs.py``;
this file focuses on the unit-testable seams.

Coverage focus:

- ``_resolve_cloud_run_job_name`` (May 2026 refactor) — replaces the
  legacy ``settings.GCS_ENERGYPLUS_JOB_NAME`` / ``GCS_FMU_JOB_NAME``
  env-var reads with a ``ValidatorConfig`` lookup. The helper is the
  single source of truth for which Cloud Run Job each validator type
  dispatches against; a regression here would either point dispatch
  at the wrong job (running stale backend code) or silently fail with
  an empty job name (the original bug that prompted the refactor).

The post-mortem on the legacy env-var path: ``GCS_*_JOB_NAME`` had
been unset in prod Secret Manager for months, defaulting to ``""``,
and the launcher was calling ``gcloud run jobs run ""`` which errored
out. The dataclass-driven path eliminates the cross-config-source
drift class of bug.
"""

from __future__ import annotations

import pytest

from validibot.validations.services.cloud_run.launcher import (
    _resolve_cloud_run_job_name,
)
from validibot.validations.validators.base.config import _CONFIG_REGISTRY
from validibot.validations.validators.base.config import ValidatorConfig


class TestResolveCloudRunJobName:
    """``_resolve_cloud_run_job_name`` derives job name from ValidatorConfig.

    These cover the happy paths (registered config returns its
    ``cloud_run_job_name``) and the two error paths the helper is
    explicit about — both must raise ``RuntimeError`` with actionable
    messages so an operator can diagnose a misconfiguration without
    digging through stack traces.
    """

    def test_returns_job_name_for_registered_validator(self):
        """Registered validator → its cloud_run_job_name.

        Uses the real EnergyPlus registration (populated in
        ``ValidationsConfig.ready()``) so the test exercises the full
        registry-lookup path rather than mocking it. EnergyPlus is
        chosen because it has an explicit ``image_name`` set in
        config.py — covers the property's "use image_name" branch.
        """
        result = _resolve_cloud_run_job_name("ENERGYPLUS")
        assert result == "validibot-validator-backend-energyplus"

    def test_returns_job_name_for_fmu(self):
        """FMU resolves to its own backend job name.

        Symmetric coverage for the second system advanced validator
        — guards against a regression that special-cases EnergyPlus
        without handling FMU (which is exactly what the legacy
        env-var path used to do: two separate ``GCS_*_JOB_NAME``
        reads, one per validator).
        """
        result = _resolve_cloud_run_job_name("FMU")
        assert result == "validibot-validator-backend-fmu"

    def test_raises_when_no_config_registered(self):
        """Unknown validation_type → RuntimeError with diagnostic message.

        Pins the "config-missing" error path. The message must mention
        the unknown ``validation_type`` so the operator can immediately
        see which call site is misconfigured. Importantly, the helper
        does NOT silently fall back to the convention here — an unknown
        type usually means a bug in the dispatch layer (not "user
        registered a new validator with no name").
        """
        with pytest.raises(RuntimeError) as exc_info:
            _resolve_cloud_run_job_name("DEFINITELY_NOT_REGISTERED")

        assert "DEFINITELY_NOT_REGISTERED" in str(exc_info.value)
        assert "No ValidatorConfig registered" in str(exc_info.value)

    def test_raises_when_config_yields_empty_job_name(self):
        """Registered config that produces empty job name → RuntimeError.

        The fallback convention (`validibot-validator-backend-{slug}`)
        derives from ``validation_type.lower()``. If a config somehow
        has BOTH empty ``image_name`` AND empty ``validation_type``
        (which Pydantic shouldn't allow but defensive code protects
        anyway), the property returns "" and the helper must reject
        it rather than launching a job with an empty name.

        Uses a temp config registered against a real registry slot
        and rolled back in ``finally`` so test isolation holds.
        """
        # Build a config whose validation_type is empty and image_name
        # is empty — pathological case that yields cloud_run_job_name
        # of "validibot-validator-backend-".
        sentinel_type = ""
        original_value = _CONFIG_REGISTRY.get(sentinel_type)

        ValidatorConfig(
            slug="empty-test",
            name="Empty Test",
            validation_type=sentinel_type,
            validator_class=(
                "validibot.validations.validators.basic.validator.BasicValidator"
            ),
            # image_name and validation_type both empty → property
            # falls back to "validibot-validator-backend-" (with empty
            # slug suffix). That's a hyphen-suffixed string, NOT empty,
            # so technically it doesn't trigger the empty-name branch.
            # Bypass by clearing the registry entry's name post-hoc
            # via a deliberate test fixture (cleaner than mocking).
        )

        try:
            # Directly populate the registry to skip register_validator_config's
            # validation pipeline (which would reject this config).
            # This is a deliberate sharp-edge test of the helper's
            # defensive guard — not a real configuration the registry
            # could produce via the normal API.
            class _EmptyJobNameConfig:
                cloud_run_job_name = ""

            _CONFIG_REGISTRY[sentinel_type] = _EmptyJobNameConfig()

            with pytest.raises(RuntimeError) as exc_info:
                _resolve_cloud_run_job_name(sentinel_type)

            assert "empty Cloud Run Job name" in str(exc_info.value)
        finally:
            # Restore registry state; don't pollute other tests.
            if original_value is None:
                _CONFIG_REGISTRY.pop(sentinel_type, None)
            else:
                _CONFIG_REGISTRY[sentinel_type] = original_value


# NOTE: an earlier draft of this file included a
# ``TestResolveCloudRunJobNameWithCustomConfig`` class that exercised
# ``register_validator_config()`` directly. It was dropped because that
# function mutates the resolved validator class's ``validation_type``
# attribute as a side effect of registration, which broke
# ``test_class_has_validation_type_attribute`` in
# ``tests/test_validators/test_registry.py`` (a parametrised test that
# walks every registered ValidationType). The same code path is already
# exercised end-to-end by the system EnergyPlus / FMU registrations in
# the tests above — no coverage is lost.
