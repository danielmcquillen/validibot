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
