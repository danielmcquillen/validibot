"""
Tests for the AdvancedValidator base class.

Uses a concrete stub subclass (_StubAdvancedValidator) that implements
the two abstract methods with controllable behavior to verify the
template method lifecycle for container-based validators.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

from django.test import TestCase

from validibot.actions.protocols import RunContext
from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.services.execution.base import ExecutionResponse
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.factories import WorkflowStepFactory
from validibot.validations.validators.base.advanced import AdvancedValidator


class _StubAdvancedValidator(AdvancedValidator):
    """
    Concrete stub for testing the AdvancedValidator template method lifecycle.

    Implements the two abstract methods (validator_display_name and
    extract_output_signals) with configurable behavior to exercise
    the base class orchestration without needing real containers.
    """

    def __init__(self, *, signals: dict[str, Any] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._signals = signals

    @property
    def validator_display_name(self) -> str:
        return "Stub"

    @classmethod
    def extract_output_signals(cls, output_envelope: Any) -> dict[str, Any] | None:
        """Extract signals from the stub envelope's _signals attribute."""
        return getattr(output_envelope, "_test_signals", None)


def _make_mock_backend(*, is_async: bool = False, is_available: bool = True):
    """Create a mock ExecutionBackend with configurable properties."""
    backend = MagicMock()
    backend.is_async = is_async
    backend.is_available.return_value = is_available
    backend.backend_name = "MockBackend"
    return backend


def _make_output_envelope(
    *,
    status: str = "success",
    messages: list | None = None,
    outputs: Any = None,
    test_signals: dict[str, Any] | None = None,
):
    """
    Create a mock output envelope matching the ValidationOutputEnvelope interface.

    Uses a MagicMock that behaves like a Pydantic model with the fields
    that post_execute_validate() and _process_output_envelope() access.
    """
    from validibot_shared.validations.envelopes import ValidationStatus

    status_map = {
        "success": ValidationStatus.SUCCESS,
        "failed_validation": ValidationStatus.FAILED_VALIDATION,
        "failed_runtime": ValidationStatus.FAILED_RUNTIME,
    }

    envelope = MagicMock()
    envelope.status = status_map.get(status, ValidationStatus.SUCCESS)
    envelope.messages = messages or []
    envelope.outputs = outputs
    # Custom attribute for the stub's extract_output_signals()
    envelope._test_signals = test_signals
    return envelope


def _make_envelope_message(*, text: str, severity: str = "ERROR", path: str = ""):
    """Create a mock ValidationMessage for envelope messages."""
    from validibot_shared.validations.envelopes import Severity as EnvelopeSeverity

    severity_map = {
        "ERROR": EnvelopeSeverity.ERROR,
        "WARNING": EnvelopeSeverity.WARNING,
        "INFO": EnvelopeSeverity.INFO,
    }
    msg = MagicMock()
    msg.text = text
    msg.severity = severity_map[severity]
    if path:
        msg.location = MagicMock()
        msg.location.path = path
    else:
        msg.location = None
    return msg


class AdvancedValidatorValidateTests(TestCase):
    """Tests the template method validate() lifecycle on AdvancedValidator."""

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        cls.project = ProjectFactory(org=cls.org)
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            org=cls.org,
            is_system=False,
        )
        cls.ruleset = RulesetFactory(
            org=cls.org,
            ruleset_type=RulesetType.ENERGYPLUS,
        )
        cls.submission = SubmissionFactory(
            org=cls.org,
            project=cls.project,
            user=cls.user,
            file_type=SubmissionFileType.BINARY,
        )
        cls.step = WorkflowStepFactory(
            validator=cls.validator,
            ruleset=cls.ruleset,
        )

    def test_validate_returns_error_when_run_context_is_none(self):
        """Missing run_context returns passed=False with a descriptive error."""
        engine = _StubAdvancedValidator()

        result = engine.validate(
            self.validator,
            self.submission,
            self.ruleset,
            run_context=None,
        )

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].severity, Severity.ERROR)
        self.assertIn("workflow context", result.issues[0].message)

    def test_validate_returns_error_when_run_context_missing_run(self):
        """RunContext with no validation_run returns passed=False."""
        engine = _StubAdvancedValidator()
        context = RunContext(validation_run=None, step=self.step)

        result = engine.validate(
            self.validator,
            self.submission,
            self.ruleset,
            run_context=context,
        )

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("workflow context", result.issues[0].message)

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_validate_returns_error_when_backend_not_available(
        self,
        mock_get_backend,
    ):
        """When execution backend is unavailable, returns passed=False."""
        mock_get_backend.return_value = _make_mock_backend(is_available=False)
        engine = _StubAdvancedValidator()
        context = RunContext(
            validation_run=MagicMock(),
            step=self.step,
        )

        result = engine.validate(
            self.validator,
            self.submission,
            self.ruleset,
            run_context=context,
        )

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].severity, Severity.ERROR)
        self.assertIn("not available", result.issues[0].message)

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_validate_sync_success_returns_passed(self, mock_get_backend):
        """Sync backend with a SUCCESS envelope returns passed=True."""
        envelope = _make_output_envelope(status="success")
        response = ExecutionResponse(
            execution_id="exec-123",
            is_complete=True,
            output_envelope=envelope,
            input_uri="file:///input.json",
            output_uri="file:///output.json",
            execution_bundle_uri="file:///bundle/",
        )
        backend = _make_mock_backend(is_async=False)
        backend.execute.return_value = response
        mock_get_backend.return_value = backend

        engine = _StubAdvancedValidator()
        context = RunContext(
            validation_run=MagicMock(),
            step=self.step,
        )

        result = engine.validate(
            self.validator,
            self.submission,
            self.ruleset,
            run_context=context,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.stats["execution_id"], "exec-123")
        self.assertFalse(result.stats["is_async"])
        backend.execute.assert_called_once()

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_validate_async_pending_returns_none_passed(self, mock_get_backend):
        """Async backend with incomplete response returns passed=None (pending)."""
        response = ExecutionResponse(
            execution_id="job-456",
            is_complete=False,
            input_uri="gs://bucket/input.json",
            output_uri="gs://bucket/output.json",
            execution_bundle_uri="gs://bucket/bundle/",
        )
        backend = _make_mock_backend(is_async=True)
        backend.execute.return_value = response
        mock_get_backend.return_value = backend

        engine = _StubAdvancedValidator()
        context = RunContext(
            validation_run=MagicMock(),
            step=self.step,
        )

        result = engine.validate(
            self.validator,
            self.submission,
            self.ruleset,
            run_context=context,
        )

        self.assertIsNone(result.passed)
        self.assertEqual(result.issues, [])
        self.assertTrue(result.stats["is_async"])

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_validate_sync_error_message_returns_failed(self, mock_get_backend):
        """Sync response with error_message returns passed=False."""
        response = ExecutionResponse(
            execution_id="exec-err",
            is_complete=True,
            error_message="Container exited with code 1",
            input_uri="file:///input.json",
            output_uri="file:///output.json",
            execution_bundle_uri="file:///bundle/",
        )
        backend = _make_mock_backend(is_async=False)
        backend.execute.return_value = response
        mock_get_backend.return_value = backend

        engine = _StubAdvancedValidator()
        context = RunContext(
            validation_run=MagicMock(),
            step=self.step,
        )

        result = engine.validate(
            self.validator,
            self.submission,
            self.ruleset,
            run_context=context,
        )

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("Container exited", result.issues[0].message)

    @patch("validibot.validations.services.execution.get_execution_backend")
    def test_validate_sync_no_envelope_returns_failed(self, mock_get_backend):
        """Sync response with no output envelope returns passed=False."""
        response = ExecutionResponse(
            execution_id="exec-empty",
            is_complete=True,
            output_envelope=None,
            input_uri="file:///input.json",
            output_uri="file:///output.json",
            execution_bundle_uri="file:///bundle/",
        )
        backend = _make_mock_backend(is_async=False)
        backend.execute.return_value = response
        mock_get_backend.return_value = backend

        engine = _StubAdvancedValidator()
        context = RunContext(
            validation_run=MagicMock(),
            step=self.step,
        )

        result = engine.validate(
            self.validator,
            self.submission,
            self.ruleset,
            run_context=context,
        )

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("no output envelope", result.issues[0].message)


class AdvancedValidatorPostExecuteTests(TestCase):
    """Tests the post_execute_validate() method for output envelope processing."""

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            org=cls.org,
            is_system=False,
        )
        cls.ruleset = RulesetFactory(
            org=cls.org,
            ruleset_type=RulesetType.ENERGYPLUS,
        )
        cls.step = WorkflowStepFactory(
            validator=cls.validator,
            ruleset=cls.ruleset,
        )

    def test_success_envelope_with_no_messages_passes(self):
        """SUCCESS envelope with no messages returns passed=True."""
        envelope = _make_output_envelope(
            status="success",
            test_signals={"site_eui": 75.2},
        )
        engine = _StubAdvancedValidator()

        result = engine.post_execute_validate(envelope, run_context=None)

        self.assertTrue(result.passed)
        self.assertEqual(result.issues, [])
        self.assertEqual(result.signals, {"site_eui": 75.2})

    def test_failed_validation_envelope_returns_failed(self):
        """FAILED_VALIDATION envelope returns passed=False."""
        envelope = _make_output_envelope(status="failed_validation")
        engine = _StubAdvancedValidator()

        result = engine.post_execute_validate(envelope, run_context=None)

        self.assertFalse(result.passed)

    def test_failed_runtime_envelope_returns_failed(self):
        """FAILED_RUNTIME envelope returns passed=False."""
        envelope = _make_output_envelope(status="failed_runtime")
        engine = _StubAdvancedValidator()

        result = engine.post_execute_validate(envelope, run_context=None)

        self.assertFalse(result.passed)

    def test_envelope_messages_converted_to_issues(self):
        """Messages from the envelope are converted to ValidationIssue instances."""
        messages = [
            _make_envelope_message(
                text="Severe error in simulation",
                severity="ERROR",
                path="Building/Zone1",
            ),
            _make_envelope_message(
                text="Unused schedule detected",
                severity="WARNING",
            ),
        ]
        envelope = _make_output_envelope(status="success", messages=messages)
        engine = _StubAdvancedValidator()

        result = engine.post_execute_validate(envelope, run_context=None)

        self.assertEqual(len(result.issues), 2)
        self.assertEqual(result.issues[0].message, "Severe error in simulation")
        self.assertEqual(result.issues[0].severity, Severity.ERROR)
        self.assertEqual(result.issues[0].path, "Building/Zone1")
        self.assertEqual(result.issues[1].message, "Unused schedule detected")
        self.assertEqual(result.issues[1].severity, Severity.WARNING)
        self.assertEqual(result.issues[1].path, "")

    def test_extract_output_signals_called_correctly(self):
        """Signals from extract_output_signals() appear in result.signals."""
        envelope = _make_output_envelope(
            status="success",
            test_signals={"site_eui_kwh_m2": 120.5, "peak_demand_w": 5000},
        )
        engine = _StubAdvancedValidator()

        result = engine.post_execute_validate(envelope, run_context=None)

        self.assertEqual(
            result.signals,
            {"site_eui_kwh_m2": 120.5, "peak_demand_w": 5000},
        )

    def test_no_signals_returns_empty_dict(self):
        """When extract_output_signals() returns None, signals is empty dict."""
        envelope = _make_output_envelope(status="success", test_signals=None)
        engine = _StubAdvancedValidator()

        result = engine.post_execute_validate(envelope, run_context=None)

        self.assertEqual(result.signals, {})

    def test_envelope_outputs_included_in_stats(self):
        """Envelope outputs are serialized into result.stats."""
        outputs_mock = MagicMock()
        outputs_mock.model_dump.return_value = {"returncode": 0, "log": "done"}
        envelope = _make_output_envelope(status="success", outputs=outputs_mock)
        engine = _StubAdvancedValidator()

        result = engine.post_execute_validate(envelope, run_context=None)

        self.assertEqual(
            result.stats["outputs"],
            {"returncode": 0, "log": "done"},
        )

    def test_assertion_stats_default_to_zero(self):
        """Without run_context, assertion_stats are zero."""
        envelope = _make_output_envelope(status="success")
        engine = _StubAdvancedValidator()

        result = engine.post_execute_validate(envelope, run_context=None)

        self.assertEqual(result.assertion_stats.total, 0)
        self.assertEqual(result.assertion_stats.failures, 0)

    def test_success_envelope_with_error_messages_still_passes(self):
        """SUCCESS status passes even if envelope contains ERROR messages.

        Container error messages do not override SUCCESS status. Only
        assertion failures cause a SUCCESS envelope to fail.
        """
        messages = [
            _make_envelope_message(
                text="Non-fatal simulation warning treated as error",
                severity="ERROR",
            ),
        ]
        envelope = _make_output_envelope(status="success", messages=messages)
        engine = _StubAdvancedValidator()

        result = engine.post_execute_validate(envelope, run_context=None)

        # passed=True because status is SUCCESS and there are no assertion failures
        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 1)


class AdvancedValidatorHelperTests(TestCase):
    """Tests for the extracted static helper methods on AdvancedValidator.

    These helpers were extracted from duplicated logic in
    ``post_execute_validate()`` and ``_process_output_envelope()`` to
    ensure both code paths share the same behavior.  Testing them
    independently verifies the extraction preserved correctness and
    allows each method to be tested in isolation without constructing
    a full envelope workflow.
    """

    # ── _extract_issues_from_envelope ─────────────────────────────

    def test_extract_issues_maps_severities_correctly(self):
        """Each envelope severity maps to the correct local Severity."""
        messages = [
            _make_envelope_message(text="err", severity="ERROR", path="a.idf"),
            _make_envelope_message(text="warn", severity="WARNING"),
            _make_envelope_message(text="info", severity="INFO"),
        ]
        envelope = _make_output_envelope(status="success", messages=messages)

        issues = _StubAdvancedValidator._extract_issues_from_envelope(envelope)

        self.assertEqual(len(issues), 3)
        self.assertEqual(issues[0].severity, Severity.ERROR)
        self.assertEqual(issues[0].path, "a.idf")
        self.assertEqual(issues[0].message, "err")
        self.assertEqual(issues[1].severity, Severity.WARNING)
        self.assertEqual(issues[1].path, "")
        self.assertEqual(issues[2].severity, Severity.INFO)

    def test_extract_issues_empty_messages(self):
        """An envelope with no messages yields an empty issue list."""
        envelope = _make_output_envelope(status="success", messages=[])
        issues = _StubAdvancedValidator._extract_issues_from_envelope(envelope)
        self.assertEqual(issues, [])

    # ── _determine_passed ─────────────────────────────────────────

    def test_determine_passed_success_no_failures(self):
        """SUCCESS with zero assertion failures returns True."""
        envelope = _make_output_envelope(status="success")
        self.assertTrue(
            _StubAdvancedValidator._determine_passed(envelope, assertion_failures=0)
        )

    def test_determine_passed_success_with_failures(self):
        """SUCCESS with assertion failures returns False."""
        envelope = _make_output_envelope(status="success")
        self.assertFalse(
            _StubAdvancedValidator._determine_passed(envelope, assertion_failures=1)
        )

    def test_determine_passed_failed_validation(self):
        """FAILED_VALIDATION always returns False regardless of assertions."""
        envelope = _make_output_envelope(status="failed_validation")
        self.assertFalse(
            _StubAdvancedValidator._determine_passed(envelope, assertion_failures=0)
        )

    def test_determine_passed_failed_runtime(self):
        """FAILED_RUNTIME always returns False."""
        envelope = _make_output_envelope(status="failed_runtime")
        self.assertFalse(
            _StubAdvancedValidator._determine_passed(envelope, assertion_failures=0)
        )

    def test_determine_passed_default_zero_failures(self):
        """When assertion_failures is omitted, it defaults to 0."""
        envelope = _make_output_envelope(status="success")
        self.assertTrue(_StubAdvancedValidator._determine_passed(envelope))

    # ── _extract_outputs_stats ────────────────────────────────────

    def test_extract_outputs_stats_with_pydantic_model(self):
        """Pydantic-like outputs (with model_dump) are serialized to stats."""
        outputs_mock = MagicMock()
        outputs_mock.model_dump.return_value = {"metric": 42}
        envelope = _make_output_envelope(status="success", outputs=outputs_mock)

        stats = _StubAdvancedValidator._extract_outputs_stats(envelope)

        self.assertEqual(stats, {"outputs": {"metric": 42}})

    def test_extract_outputs_stats_with_dict(self):
        """Dict outputs are passed through directly."""
        envelope = _make_output_envelope(status="success")
        envelope.outputs = {"raw": "data"}

        stats = _StubAdvancedValidator._extract_outputs_stats(envelope)

        self.assertEqual(stats, {"outputs": {"raw": "data"}})

    def test_extract_outputs_stats_none(self):
        """None outputs return an empty dict."""
        envelope = _make_output_envelope(status="success", outputs=None)

        stats = _StubAdvancedValidator._extract_outputs_stats(envelope)

        self.assertEqual(stats, {})


# ==============================================================================
# _build_assertion_payload — nested output namespace
#
# Output-stage assertions can reference both user inputs (from the submission
# JSON) and validator outputs (from extract_output_signals).  The payload
# merges these into a single dict with a nested ``output`` namespace so that
# CEL member access (``output.T_room``) and basic-assertion dot-path
# navigation (``output.T_room``) both resolve correctly.
# ==============================================================================


class BuildAssertionPayloadTests(TestCase):
    """Tests for the merged assertion payload structure.

    The ``_build_assertion_payload`` static method combines submission
    input values with output signals for evaluation at the output stage.
    The nested ``output`` namespace ensures that ``output.<name>``
    resolves via both CEL member access and basic-assertion dot-path
    navigation.
    """

    def _build(self, signals, content_json=None):
        """Call _build_assertion_payload with a mock RunContext.

        Args:
            signals: Output signals dict (from extract_output_signals).
            content_json: Optional JSON string for the submission content.
                If None, the submission mock returns None for get_content().
        """
        run_context = MagicMock()
        submission = MagicMock()
        submission.get_content.return_value = content_json
        run_context.validation_run = MagicMock()
        run_context.validation_run.submission = submission
        return _StubAdvancedValidator._build_assertion_payload(signals, run_context)

    def test_no_submission_content_returns_signals(self):
        """When the submission has no content, only output signals are
        returned — no nested namespace since there's nothing to merge.
        """
        result = self._build({"T_room": 296.63}, content_json=None)
        self.assertEqual(result, {"T_room": 296.63})

    def test_non_json_content_returns_signals(self):
        """Non-JSON submission content (e.g., EnergyPlus IDF files)
        can't be merged with output signals.
        """
        result = self._build({"T_room": 296.63}, content_json="!- IDF file")
        self.assertEqual(result, {"T_room": 296.63})

    def test_no_collision_bare_names_and_namespace(self):
        """When there are no name collisions between inputs and outputs,
        all values are available under bare names AND outputs are in the
        nested ``output`` namespace.

        This means ``Q_cooling_actual`` and ``output.Q_cooling_actual``
        both work in CEL expressions.
        """
        result = self._build(
            {"T_room": 296.63, "Q_cooling_actual": 5172.83},
            content_json='{"Q_cooling_max": 6000, "T_setpoint": 295}',
        )
        # Inputs under bare names
        self.assertEqual(result["Q_cooling_max"], 6000)
        self.assertEqual(result["T_setpoint"], 295)
        # Outputs under bare names (no collision)
        self.assertEqual(result["T_room"], 296.63)
        self.assertEqual(result["Q_cooling_actual"], 5172.83)
        # All outputs also in nested namespace
        self.assertIn("output", result)
        self.assertEqual(result["output"]["T_room"], 296.63)
        self.assertEqual(result["output"]["Q_cooling_actual"], 5172.83)

    def test_collision_input_keeps_bare_name(self):
        """When a submission key collides with an output signal name,
        the input value keeps the bare name and the output is only
        reachable via the nested ``output`` namespace.

        This is the key design decision: bare ``T_room`` resolves to
        the input value, and ``output.T_room`` resolves to the output.
        """
        result = self._build(
            {"T_room": 296.63},
            content_json='{"T_room": 293.15, "T_setpoint": 295}',
        )
        # Bare name keeps input value
        self.assertEqual(result["T_room"], 293.15)
        # Output via nested namespace
        self.assertEqual(result["output"]["T_room"], 296.63)
        # Non-colliding input still under bare name
        self.assertEqual(result["T_setpoint"], 295)

    def test_no_run_context_returns_signals_copy(self):
        """Without run_context, a plain copy of signals is returned."""
        signals = {"T_room": 296.63}
        result = _StubAdvancedValidator._build_assertion_payload(signals, None)
        self.assertEqual(result, {"T_room": 296.63})
        # Must be a copy, not the original dict
        self.assertIsNot(result, signals)

    def test_non_dict_json_returns_signals(self):
        """JSON content that isn't a dict (e.g., an array) can't be
        merged — only the output signals are returned.
        """
        result = self._build({"T_room": 296.63}, content_json="[1, 2, 3]")
        self.assertEqual(result, {"T_room": 296.63})

    # ── resolved_inputs parameter ────────────────────────────────
    #
    # When resolved_inputs is provided, it takes precedence over raw
    # submission JSON.  This ensures assertions see the same values
    # (with defaults applied, nested paths resolved) that the
    # validator launch used.

    def test_resolved_inputs_used_instead_of_raw_submission(self):
        """When resolved_inputs is provided, it is used for input values
        rather than parsing the raw submission JSON.

        This is the core fix: a StepSignalBinding may define a default
        value for an input signal.  If the submission omits that key,
        resolve_step_input_signals() fills it in.  The assertion payload
        must see the resolved value, not the raw (missing) one.
        """
        signals = {"T_room": 296.63}
        resolved = {"Q_cooling_max": 6000, "panel_area": 12.5}
        # Raw submission is missing Q_cooling_max — but resolved_inputs has it.
        result = _StubAdvancedValidator._build_assertion_payload(
            signals,
            run_context=None,
            resolved_inputs=resolved,
        )
        self.assertEqual(result["Q_cooling_max"], 6000)
        self.assertEqual(result["panel_area"], 12.5)
        self.assertEqual(result["T_room"], 296.63)
        self.assertEqual(result["output"]["T_room"], 296.63)

    def test_resolved_inputs_collision_input_keeps_bare_name(self):
        """When resolved_inputs contains a key that collides with an
        output signal, the input value keeps the bare name (same
        convention as the raw-submission path).
        """
        signals = {"T_room": 296.63}
        resolved = {"T_room": 293.15, "T_setpoint": 295}
        result = _StubAdvancedValidator._build_assertion_payload(
            signals,
            run_context=None,
            resolved_inputs=resolved,
        )
        # Bare name keeps the input (resolved) value
        self.assertEqual(result["T_room"], 293.15)
        # Output reachable via namespace
        self.assertEqual(result["output"]["T_room"], 296.63)
        self.assertEqual(result["T_setpoint"], 295)

    def test_empty_resolved_inputs_falls_back_to_submission(self):
        """An empty resolved_inputs dict is treated as falsy, so the
        method falls back to the raw submission JSON.  This preserves
        backward compatibility for legacy steps without
        StepSignalBinding rows.
        """
        result = self._build(
            {"T_room": 296.63},
            content_json='{"Q_cooling_max": 6000}',
        )
        # Falls back to submission JSON
        self.assertEqual(result["Q_cooling_max"], 6000)
        self.assertEqual(result["T_room"], 296.63)

    def test_none_resolved_inputs_falls_back_to_submission(self):
        """When resolved_inputs is explicitly None, the method falls
        back to the raw submission JSON (backward compatibility).
        """
        signals = {"T_room": 296.63}
        run_context = MagicMock()
        submission = MagicMock()
        submission.get_content.return_value = '{"Q_cooling_max": 6000}'
        run_context.validation_run = MagicMock()
        run_context.validation_run.submission = submission
        result = _StubAdvancedValidator._build_assertion_payload(
            signals,
            run_context,
            resolved_inputs=None,
        )
        self.assertEqual(result["Q_cooling_max"], 6000)
        self.assertEqual(result["T_room"], 296.63)


# ==============================================================================
# _get_resolved_inputs — step_run lookup
#
# The _get_resolved_inputs helper retrieves resolved input values from
# step_run.output, where the envelope builder stores them after
# resolve_step_input_signals() runs.  This enables output-stage
# assertions to see the same values (with defaults, nested paths) that
# the validator launch used.
# ==============================================================================


class GetResolvedInputsTests(TestCase):
    """Tests for the _get_resolved_inputs static method.

    This method looks up the ValidationStepRun for the current run/step
    pair and returns the ``resolved_inputs`` dict stored in
    ``step_run.output`` by the envelope builder.
    """

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        cls.project = ProjectFactory(org=cls.org)
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            org=cls.org,
            is_system=False,
        )
        cls.ruleset = RulesetFactory(
            org=cls.org,
            ruleset_type=RulesetType.ENERGYPLUS,
        )
        cls.step = WorkflowStepFactory(
            validator=cls.validator,
            ruleset=cls.ruleset,
        )

    def test_returns_resolved_inputs_from_step_run(self):
        """When the step_run has resolved_inputs in its output, they
        are returned for use in assertion payload building.

        This is the primary path: the envelope builder stored the fully
        resolved input values (with defaults and nested-path resolution)
        on step_run.output['resolved_inputs'] during FMU envelope
        construction.
        """
        from validibot.validations.tests.factories import ValidationRunFactory
        from validibot.validations.tests.factories import ValidationStepRunFactory

        run = ValidationRunFactory(
            workflow=self.step.workflow,
            org=self.org,
        )
        resolved = {"Q_cooling_max": 6000, "panel_area": 12.5}
        ValidationStepRunFactory(
            validation_run=run,
            workflow_step=self.step,
            output={"resolved_inputs": resolved},
        )
        run_context = RunContext(validation_run=run, step=self.step)

        result = _StubAdvancedValidator._get_resolved_inputs(run_context)

        self.assertEqual(result, {"Q_cooling_max": 6000, "panel_area": 12.5})

    def test_returns_none_when_no_step_run(self):
        """When no step_run exists for the run/step pair, returns None
        so the caller can fall back to raw submission JSON.
        """
        from validibot.validations.tests.factories import ValidationRunFactory

        run = ValidationRunFactory(
            workflow=self.step.workflow,
            org=self.org,
        )
        run_context = RunContext(validation_run=run, step=self.step)

        result = _StubAdvancedValidator._get_resolved_inputs(run_context)

        self.assertIsNone(result)

    def test_returns_none_when_no_resolved_inputs_key(self):
        """When step_run.output exists but has no 'resolved_inputs' key
        (e.g., legacy steps or EnergyPlus preprocessing metadata), returns
        None for backward compatibility.
        """
        from validibot.validations.tests.factories import ValidationRunFactory
        from validibot.validations.tests.factories import ValidationStepRunFactory

        run = ValidationRunFactory(
            workflow=self.step.workflow,
            org=self.org,
        )
        ValidationStepRunFactory(
            validation_run=run,
            workflow_step=self.step,
            output={"template_parameters_used": {"param1": "value1"}},
        )
        run_context = RunContext(validation_run=run, step=self.step)

        result = _StubAdvancedValidator._get_resolved_inputs(run_context)

        self.assertIsNone(result)

    def test_returns_none_when_run_context_is_none(self):
        """A None run_context returns None (no crash)."""
        result = _StubAdvancedValidator._get_resolved_inputs(None)
        self.assertIsNone(result)

    def test_returns_none_when_validation_run_is_none(self):
        """A run_context with no validation_run returns None."""
        run_context = RunContext(validation_run=None, step=self.step)
        result = _StubAdvancedValidator._get_resolved_inputs(run_context)
        self.assertIsNone(result)
