"""
Tests for the workflow-level signal resolution service.

This suite validates ``resolve_workflow_signals``, ``validate_signal_name``,
and ``validate_signal_name_unique`` -- the three public functions in
``validibot.validations.services.signal_resolution``.

Why this matters: workflow signal mappings are the pre-step resolution phase
that runs before any workflow step executes.  If a mapping silently resolves
to the wrong value (or fails to raise when a required mapping is missing),
the downstream CEL expressions will operate on incorrect data without any
obvious error.  These tests lock down the contract between submission
payloads and the ``s`` / ``signal`` CEL namespace.

``validate_signal_name`` guards the author-facing signal naming rules: CEL
identifier syntax and reserved-name rejection.  If invalid names slip
through, CEL evaluation will fail with confusing parse errors instead of
a clear validation message.

``validate_signal_name_unique`` enforces cross-table uniqueness for signal
names within a workflow, spanning both ``WorkflowSignalMapping`` rows and
``SignalDefinition`` promoted outputs.  Without this, two signals could
silently shadow each other in the CEL namespace.
"""

from __future__ import annotations

import pytest

from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.services.signal_resolution import RESERVED_CEL_NAMES
from validibot.validations.services.signal_resolution import SignalResolutionError
from validibot.validations.services.signal_resolution import resolve_workflow_signals
from validibot.validations.services.signal_resolution import validate_signal_name
from validibot.validations.services.signal_resolution import validate_signal_name_unique
from validibot.validations.tests.factories import SignalDefinitionFactory
from validibot.workflows.models import WorkflowSignalMapping
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

EXPECTED_FILTERED_EMISSIVITY = 0.9

# ── resolve_workflow_signals ───────────────────────────────────────────
# These tests exercise the main resolution loop that maps submission data
# into the ``s`` CEL namespace.  They cover the happy path, nested paths,
# on_missing behaviour, and default-value semantics.


@pytest.mark.django_db
class TestResolveWorkflowSignals:
    """Tests for resolve_workflow_signals()."""

    def test_happy_path_resolves_flat_keys(self):
        """Flat top-level payload keys should resolve directly to signal
        values.  This is the simplest case and the most common mapping
        pattern authors will use.
        """
        workflow = WorkflowFactory()
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="emissivity",
            on_missing="error",
            position=0,
        )
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="area",
            source_path="area",
            on_missing="error",
            position=1,
        )

        result = resolve_workflow_signals(
            workflow,
            {"emissivity": 0.85, "area": 50},
        )

        assert result.signals == {"emissivity": 0.85, "area": 50}
        assert result.errors == []

    def test_nested_path_resolution(self):
        """Dotted paths with bracket notation should traverse nested dicts
        and lists correctly.  Authors need this to reach values buried
        inside complex submission payloads (e.g. ``materials[0].emissivity``).
        """
        workflow = WorkflowFactory()
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
            on_missing="error",
            position=0,
        )

        submission = {
            "materials": [
                {"emissivity": 0.92, "name": "glass"},
                {"emissivity": 0.50, "name": "steel"},
            ],
        }

        result = resolve_workflow_signals(workflow, submission)

        assert result.signals == {"emissivity": 0.92}

    def test_missing_required_signal_raises(self):
        """When ``on_missing=error`` and the source path does not exist in
        the submission, a ``SignalResolutionError`` must be raised.
        Silently continuing with missing required data would produce
        incorrect validation results.
        """
        workflow = WorkflowFactory()
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="missing.path",
            on_missing="error",
            position=0,
        )

        with pytest.raises(SignalResolutionError):
            resolve_workflow_signals(workflow, {"other_key": 1})

    def test_missing_optional_signal_returns_null(self):
        """When ``on_missing=null`` and the source path is absent, the
        signal should resolve to ``None``.  This lets authors guard with
        ``s.name != null`` in CEL expressions rather than failing the
        whole run.
        """
        workflow = WorkflowFactory()
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="optional_param",
            source_path="does.not.exist",
            on_missing="null",
            position=0,
        )

        result = resolve_workflow_signals(workflow, {"unrelated": 42})

        assert result.signals == {"optional_param": None}
        assert result.errors == []

    def test_default_value_used_when_path_missing(self):
        """A mapping with a ``default_value`` should use it when the source
        path is absent, regardless of ``on_missing``.  This prevents
        unnecessary run failures for optional parameters with known
        sensible defaults.
        """
        workflow = WorkflowFactory()
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="area",
            source_path="building.floor_area",
            on_missing="error",
            default_value=50.0,
            position=0,
        )

        result = resolve_workflow_signals(workflow, {"building": {}})

        assert result.signals == {"area": 50.0}
        assert result.errors == []

    def test_default_value_not_used_when_path_found(self):
        """When the source path IS found, the actual value must take
        precedence over the default.  This ensures defaults are truly
        fallbacks, not overrides.
        """
        workflow = WorkflowFactory()
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="area",
            source_path="floor_area",
            on_missing="error",
            default_value=50.0,
            position=0,
        )

        result = resolve_workflow_signals(
            workflow,
            {"floor_area": 120.0},
        )

        assert result.signals == {"area": 120.0}

    def test_empty_workflow_returns_empty_dict(self):
        """A workflow with no signal mappings should return an empty
        ``SignalResolutionResult``.  This is the base case for workflows
        that rely solely on step-level signal bindings.
        """
        workflow = WorkflowFactory()

        result = resolve_workflow_signals(workflow, {"any": "data"})

        assert result.signals == {}
        assert result.errors == []

    def test_jsonpath_filter_expression(self):
        """Paths containing JSONPath filter syntax (``[?...]``) should be
        delegated to the JSONPath resolver instead of the simple dotted-
        path walker.  This lets authors target values inside arrays
        using attribute predicates.
        """
        workflow = WorkflowFactory()
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="ownedAttribute[?@.name=='emissivity'].defaultValue",
            on_missing="error",
            position=0,
        )

        submission = {
            "ownedAttribute": [
                {"name": "conductivity", "defaultValue": 1.5},
                {"name": "emissivity", "defaultValue": EXPECTED_FILTERED_EMISSIVITY},
            ],
        }

        result = resolve_workflow_signals(workflow, submission)

        assert result.signals["emissivity"] == EXPECTED_FILTERED_EMISSIVITY


# ── validate_signal_name ───────────────────────────────────────────────
# These tests verify the CEL identifier rules and reserved-name rejection.
# Invalid signal names would cause confusing CEL parse errors downstream,
# so catching them early at the validation layer is critical.


class TestValidateSignalName:
    """Tests for validate_signal_name()."""

    @pytest.mark.parametrize(
        "name",
        ["emissivity", "panel_area", "_private", "zone_1"],
    )
    def test_valid_cel_identifier(self, name):
        """Standard CEL identifiers (letters, digits, underscores, starting
        with a letter or underscore) should pass validation with no errors.
        """
        errors = validate_signal_name(name)
        assert errors == []

    @pytest.mark.parametrize(
        "name",
        ["1_bad", "foo-bar", "foo.bar", "$foo"],
    )
    def test_invalid_cel_identifier(self, name):
        """Names that violate CEL identifier syntax (starting with a digit,
        containing hyphens/dots/special characters) must be rejected so
        authors get a clear message instead of a confusing CEL parse error.
        """
        errors = validate_signal_name(name)
        assert len(errors) >= 1
        assert "not a valid signal name" in errors[0]

    @pytest.mark.parametrize(
        "name",
        ["payload", "output", "steps", "has", "true"],
    )
    def test_reserved_name_rejected(self, name):
        """Reserved CEL context keys (``payload``, ``output``, ``steps``,
        builtins like ``has``, ``true``, etc.) must be rejected.  If
        allowed, they would shadow built-in CEL names and cause subtle
        expression evaluation bugs.
        """
        assert name in RESERVED_CEL_NAMES, (
            f"Test assumption: '{name}' should be in RESERVED_CEL_NAMES"
        )
        errors = validate_signal_name(name)
        assert any("reserved" in e for e in errors)


# ── validate_signal_name_unique ────────────────────────────────────────
# These tests verify cross-table uniqueness checking for signal names
# within a workflow.  A duplicate name in the CEL ``s`` namespace would
# cause one signal to silently shadow the other.


@pytest.mark.django_db
class TestValidateSignalNameUnique:
    """Tests for validate_signal_name_unique()."""

    def test_unique_across_mapped_signals(self):
        """A name already used by a ``WorkflowSignalMapping`` on the same
        workflow should be reported as a duplicate.  This prevents two
        mapped signals from claiming the same ``s.<name>`` slot.
        """
        workflow = WorkflowFactory()
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="emissivity",
            on_missing="error",
            position=0,
        )

        errors = validate_signal_name_unique(workflow.id, "emissivity")

        assert len(errors) == 1
        assert "already defined" in errors[0]

    def test_unique_across_promoted_outputs(self):
        """A name already used as a ``SignalDefinition.signal_name``
        (promoted output) on a step in the same workflow should be
        reported as a duplicate.  Promoted outputs share the same ``s``
        namespace as mapped signals.
        """
        workflow = WorkflowFactory()
        step = WorkflowStepFactory(workflow=workflow)
        SignalDefinitionFactory(
            workflow_step=step,
            validator=None,
            direction=SignalDirection.OUTPUT,
            origin_kind=SignalOriginKind.CATALOG,
            signal_name="emissivity",
        )

        errors = validate_signal_name_unique(workflow.id, "emissivity")

        assert len(errors) == 1
        assert "promoted output" in errors[0]

    def test_no_conflict_returns_empty(self):
        """When the name is not used by any mapping or promoted output in
        the workflow, the function should return an empty error list,
        confirming the name is available.
        """
        workflow = WorkflowFactory()

        errors = validate_signal_name_unique(workflow.id, "brand_new_signal")

        assert errors == []
