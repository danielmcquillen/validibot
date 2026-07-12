"""Tests for immutable validation-run runtime profiles and rollout policy.

Runtime profiles are the mixed-version boundary for the execution-integrity
program.  These tests prove every accepted rung has one explicit policy, that
writers remain legacy-only during the reader-first release, and that a handler
which cannot interpret a run fences it instead of falling through to legacy
execution.
"""

from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from validibot.validations.constants import ExecutionContractVersion
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationRuntimeProfile
from validibot.validations.services.runtime_profiles import (
    UNSUPPORTED_RUNTIME_PROFILE_ERROR,
)
from validibot.validations.services.runtime_profiles import (
    UnsupportedRuntimeProfileError,
)
from validibot.validations.services.runtime_profiles import can_advance_runtime_profile
from validibot.validations.services.runtime_profiles import (
    ensure_runtime_profile_supported,
)
from validibot.validations.services.runtime_profiles import get_runtime_profile_policy
from validibot.validations.services.runtime_profiles import (
    is_runtime_profile_writer_enabled,
)
from validibot.validations.tests.factories import ValidationRunFactory


class TestRuntimeProfilePolicy:
    """Keep the four-rung rollout policy explicit and table-driven."""

    def test_every_declared_profile_has_exactly_one_policy(self):
        """A new enum value must not silently inherit another profile's behavior."""
        resolved = {
            get_runtime_profile_policy(profile).profile
            for profile in ValidationRuntimeProfile
        }

        assert resolved == set(ValidationRuntimeProfile)

    @pytest.mark.parametrize(
        ("profile", "contract", "attempts", "strict_io", "canonical_context"),
        [
            (
                ValidationRuntimeProfile.LEGACY,
                ExecutionContractVersion.LEGACY_URI_V1,
                False,
                False,
                False,
            ),
            (
                ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
                ExecutionContractVersion.LEGACY_URI_V1,
                True,
                False,
                False,
            ),
            (
                ValidationRuntimeProfile.ATTEMPT_STRICT_V1,
                ExecutionContractVersion.STRICT_CONTENT_V1,
                True,
                True,
                False,
            ),
            (
                ValidationRuntimeProfile.ATTEMPT_CONTEXT_V1,
                ExecutionContractVersion.STRICT_CONTENT_V1,
                True,
                True,
                True,
            ),
        ],
    )
    def test_profile_capabilities_match_the_accepted_ladder(
        self,
        profile,
        contract,
        attempts,
        strict_io,
        canonical_context,
    ):
        """Each rung must add only the capability accepted for that stage."""
        policy = get_runtime_profile_policy(profile)

        assert policy.contract_version == contract
        assert policy.uses_execution_attempts is attempts
        assert policy.uses_strict_io is strict_io
        assert policy.uses_canonical_context is canonical_context

    def test_progression_allows_only_same_or_next_rung(self):
        """Skipping or reversing a rung would bypass a mixed-version safety gate."""
        profiles = list(ValidationRuntimeProfile)

        for current_index, current in enumerate(profiles):
            for target_index, target in enumerate(profiles):
                expected = target_index in {current_index, current_index + 1}
                assert can_advance_runtime_profile(current, target) is expected

    def test_lifecycle_release_enables_only_completed_writer_rungs(self):
        """Strict-I/O profiles remain reader-only until their ADRs complete."""
        assert is_runtime_profile_writer_enabled(ValidationRuntimeProfile.LEGACY)
        assert is_runtime_profile_writer_enabled(
            ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1
        )
        for profile in list(ValidationRuntimeProfile)[2:]:
            assert not is_runtime_profile_writer_enabled(profile)

    def test_unknown_profile_is_rejected(self):
        """Typos or newer unknown profiles must never be interpreted as legacy."""
        with pytest.raises(UnsupportedRuntimeProfileError):
            get_runtime_profile_policy("ATTEMPT_UNKNOWN_V9")


@pytest.mark.django_db
class TestRuntimeProfilePersistence:
    """Protect a run's recorded execution semantics for its whole lifetime."""

    def test_new_runs_default_to_legacy(self):
        """The additive migration must leave existing production behavior unchanged."""
        run = ValidationRunFactory()

        assert run.runtime_profile == ValidationRuntimeProfile.LEGACY

    def test_normal_model_save_cannot_reinterpret_an_existing_run(self):
        """Changing profile in place could route an in-flight callback incorrectly."""
        run = ValidationRunFactory()
        run.runtime_profile = ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1

        with pytest.raises(ValidationError, match="cannot be changed"):
            run.save(update_fields=["runtime_profile"])

        run.refresh_from_db()
        assert run.runtime_profile == ValidationRuntimeProfile.LEGACY

    @patch("validibot.validations.signals.validation_run_finalized.send_robust")
    def test_unsupported_handler_fences_active_run_once(self, mock_finalized):
        """A mixed deployment must fail closed instead of using legacy JSON state."""
        run = ValidationRunFactory(
            runtime_profile=ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
            status=ValidationRunStatus.RUNNING,
        )

        first_result = ensure_runtime_profile_supported(
            run,
            supported_profiles=(ValidationRuntimeProfile.LEGACY,),
            operation="test_handler",
            sender=self.__class__,
        )
        second_result = ensure_runtime_profile_supported(
            run,
            supported_profiles=(ValidationRuntimeProfile.LEGACY,),
            operation="test_handler",
            sender=self.__class__,
        )

        run.refresh_from_db()
        assert first_result is False
        assert second_result is False
        assert run.status == ValidationRunStatus.FAILED
        assert run.error_category == ValidationRunErrorCategory.SYSTEM_ERROR
        assert run.error == UNSUPPORTED_RUNTIME_PROFILE_ERROR
        assert run.ended_at is not None
        mock_finalized.assert_called_once()

    @patch("validibot.validations.signals.validation_run_finalized.send_robust")
    def test_supported_handler_does_not_mutate_run(self, mock_finalized):
        """The Stage 1 guard must be invisible to every normal legacy run."""
        run = ValidationRunFactory(status=ValidationRunStatus.RUNNING)

        supported = ensure_runtime_profile_supported(
            run,
            supported_profiles=(ValidationRuntimeProfile.LEGACY,),
            operation="test_handler",
            sender=self.__class__,
        )

        run.refresh_from_db()
        assert supported is True
        assert run.status == ValidationRunStatus.RUNNING
        mock_finalized.assert_not_called()
