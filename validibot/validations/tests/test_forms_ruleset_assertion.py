"""
Tests for RulesetAssertionForm — signal targeting, CEL identifier validation,
and FMU variable collision detection.

The assertion form handles multiple signal sources: catalog entries (from
the validator's declared catalog) and step-level FMU variables (discovered
from the FMU model metadata).  Both sources participate in target resolution
for basic assertions and identifier validation for CEL expressions.

CEL expressions use a namespaced identifier convention:

- ``p.key`` / ``payload.key`` — raw submission data
- ``s.name`` / ``signals.name`` — author-defined signals
- ``output.name`` — this step's validator outputs
- ``steps.key.output.name`` — upstream step outputs

Bare identifiers (not prefixed with a namespace) are rejected unless they
are CEL builtins, literals, or single-letter loop variables.  These tests
verify that the form enforces this convention correctly.
"""

from __future__ import annotations

from django.test import TestCase

from validibot.validations.constants import AssertionType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.forms import RulesetAssertionForm
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import SignalDefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.utils import update_custom_validator


class RulesetAssertionFormTests(TestCase):
    """Tests for catalog-entry-backed assertions and CEL identifier validation."""

    def _form(self, *, validator, catalog_entries, data: dict, fmu_variables=None):
        """Build an assertion form with signal definitions."""
        return RulesetAssertionForm(
            data=data,
            catalog_entries=catalog_entries or [],
            validator=validator,
            fmu_variables=fmu_variables,
        )

    def test_cel_disallows_bare_identifiers_when_custom_targets_disabled(self):
        """Bare (un-namespaced) identifiers are rejected when custom targets
        are disabled.

        The validator requires all CEL identifiers to use namespace prefixes
        (``s.``, ``p.``, ``output.``, etc.).  ``rating`` here is bare and
        unknown, so the form should reject it with the "Bare identifiers
        are not allowed" error message.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        self.assertFalse(validator.allow_custom_assertion_targets)
        entry = SignalDefinitionFactory(validator=validator, contract_key="price")
        RulesetFactory()
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": entry.contract_key,
                "severity": Severity.ERROR,
                "cel_expression": "s.price > 0 && rating > 10",
                "when_expression": "",
            },
        )
        self.assertFalse(form._validator_allows_custom_targets())
        self.assertFalse(form.is_valid())
        self.assertIn("Bare identifiers are not allowed", str(form.errors))

    def test_cel_allows_namespaced_identifiers_when_custom_targets_enabled(self):
        """Namespaced identifiers are accepted when custom targets are enabled.

        When ``allow_custom_assertion_targets=True``, any properly namespaced
        expression (using ``p.``, ``s.``, ``output.``, etc.) is accepted
        without checking whether the signals actually exist in the catalog.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        self.assertTrue(validator.allow_custom_assertion_targets)
        entry = SignalDefinitionFactory(validator=validator, contract_key="price")
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": entry.contract_key,
                "severity": Severity.ERROR,
                "cel_expression": "p.price > 0 && s.rating > 10",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid())

    def test_update_custom_validator_persists_validator_fields(self):
        from validibot.validations.tests.factories import CustomValidatorFactory

        custom = CustomValidatorFactory()
        updated = update_custom_validator(
            custom,
            name="New Name",
            short_description="New short",
            description="New Desc",
            notes="New Notes",
            version="9.9",
            allow_custom_assertion_targets=True,
            supported_data_formats=["json"],
        )
        updated.validator.refresh_from_db()
        self.assertEqual(updated.validator.name, "New Name")
        self.assertEqual(updated.validator.short_description, "New short")
        self.assertEqual(updated.validator.description, "New Desc")
        self.assertEqual(updated.validator.version, "9.9")
        self.assertTrue(updated.validator.allow_custom_assertion_targets)
        self.assertEqual(updated.validator.supported_data_formats, ["json"])
        self.assertEqual(updated.notes, "New Notes")

    def test_target_resolution_prefers_input_without_prefix(self):
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC, is_system=False
        )
        input_entry = SignalDefinitionFactory(
            validator=validator,
            contract_key="temperature",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "temperature",
                "severity": Severity.ERROR,
                "cel_expression": "s.temperature > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid())
        # CEL expressions set target_catalog_entry to None — they declare
        # their own targets inside the expression text.
        self.assertIsNone(form.cleaned_data["target_catalog_entry"])

    def test_output_requires_prefix_on_collision(self):
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC, is_system=False
        )
        input_entry = SignalDefinitionFactory(
            validator=validator,
            contract_key="price",
            direction="input",
        )
        output_entry = SignalDefinitionFactory.build(
            validator=validator,
            contract_key="price",
            direction="output",
        )

        form = self._form(
            validator=validator,
            catalog_entries=[input_entry, output_entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "price",
                "severity": Severity.ERROR,
                "cel_expression": "s.price > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid())

        form_prefixed = self._form(
            validator=validator,
            catalog_entries=[input_entry, output_entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "output.price",
                "severity": Severity.ERROR,
                "cel_expression": "output.price > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form_prefixed.is_valid())
        # CEL expressions set target_catalog_entry to None
        self.assertIsNone(form_prefixed.cleaned_data["target_catalog_entry"])


# ==============================================================================
# FMU variable form validation
#
# Step-level FMU uploads store variable metadata (name, causality) as
# SignalDefinition rows.  The assertion form must accept these variables
# as valid targets and enforce the ``output.`` prefix convention for
# disambiguation when a name appears as both input and output.
# ==============================================================================


class FMUVariableTargetResolutionTests(TestCase):
    """Tests for basic-assertion target resolution with FMU variables.

    FMU variables are provided as step-owned SignalDefinition rows with
    origin_kind=FMU.  The form reads them from the ``catalog_entries``
    parameter (which now contains all available signal definitions).
    """

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        cls.validator.__class__.objects.filter(pk=cls.validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        cls.validator.refresh_from_db()

    def _make_fmu_signals(self, fmu_variables):
        """Create SignalDefinition objects from fmu_variables dicts."""
        from validibot.validations.constants import SignalOriginKind
        from validibot.validations.models import SignalDefinition
        from validibot.workflows.tests.factories import WorkflowStepFactory

        step = WorkflowStepFactory()
        sigs = []
        for var in fmu_variables:
            name = var["name"]
            causality = var.get("causality", "input")
            direction = "input" if causality == "input" else "output"
            sig = SignalDefinition.objects.create(
                workflow_step=step,
                contract_key=name,
                native_name=name,
                direction=direction,
                origin_kind=SignalOriginKind.FMU,
                data_type="number",
            )
            sigs.append(sig)
        return sigs

    def _fmu_form(self, *, data, fmu_variables):
        """Create a form with FMU signal definitions."""
        sigs = self._make_fmu_signals(fmu_variables)
        return RulesetAssertionForm(
            data=data,
            catalog_entries=sigs,
            validator=self.validator,
        )

    def test_bare_fmu_input_accepted(self):
        """A bare name matching only an FMU input variable is accepted.

        When there's no collision with an output of the same name, the
        user can reference the input with its plain name. With the unified
        signal model, FMU variables resolve to SignalDefinition objects.
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "Q_cooling_max",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # FMU signals now resolve to SignalDefinition objects
        self.assertIsNotNone(form.cleaned_data["resolved_signal"])
        self.assertEqual(
            form.cleaned_data["resolved_signal"].contract_key,
            "Q_cooling_max",
        )

    def test_bare_fmu_output_accepted(self):
        """A bare name matching only an FMU output variable is accepted.

        When there's no collision with an input of the same name, the
        user can reference the output with its plain name.
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "T_room",
                "operator": "lt",
                "comparison_value": "300",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNotNone(form.cleaned_data["resolved_signal"])
        self.assertEqual(
            form.cleaned_data["resolved_signal"].contract_key,
            "T_room",
        )

    def test_output_prefix_resolves_fmu_output(self):
        """``output.T_room`` resolves to the FMU output SignalDefinition.

        The ``output.`` prefix is used for explicit disambiguation.
        With the unified signal model, this resolves to a SignalDefinition.
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "output.T_room",
                "operator": "lt",
                "comparison_value": "300",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNotNone(form.cleaned_data["resolved_signal"])
        self.assertEqual(
            form.cleaned_data["resolved_signal"].contract_key,
            "T_room",
        )

    def test_collision_requires_output_prefix(self):
        """A bare name that's both an FMU input and output raises a
        collision error.

        In practice, FMI models enforce unique variable names per
        causality, but the form handles this defensively.  The user
        must write ``output.T_room`` to target the output.
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "T_room", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "T_room",
                "operator": "lt",
                "comparison_value": "300",
                "severity": Severity.ERROR,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Both an input and output", str(form.errors))

    def test_collision_resolved_with_output_prefix(self):
        """``output.T_room`` resolves to the output SignalDefinition
        even when the name collides with an input variable.
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "T_room", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "output.T_room",
                "operator": "lt",
                "comparison_value": "300",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNotNone(form.cleaned_data["resolved_signal"])
        self.assertEqual(
            form.cleaned_data["resolved_signal"].contract_key,
            "T_room",
        )


class FMUVariableCelIdentifierTests(TestCase):
    """Tests for CEL identifier validation with FMU variables.

    When ``allow_custom_assertion_targets`` is False, the form validates
    that all identifiers in a CEL expression use namespace prefixes.
    FMU variable names must be referenced with the ``s.`` (signals) or
    ``output.`` prefix; bare identifiers are rejected.
    """

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        cls.validator.__class__.objects.filter(pk=cls.validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        cls.validator.refresh_from_db()

    def _make_fmu_signals(self, fmu_variables):
        """Create SignalDefinition objects from fmu_variables dicts.

        Converts the legacy dict format (name, causality) into
        step-owned SignalDefinition rows with origin_kind=FMU.
        """
        from validibot.validations.constants import SignalOriginKind
        from validibot.validations.models import SignalDefinition
        from validibot.workflows.tests.factories import WorkflowStepFactory

        step = WorkflowStepFactory()
        sigs = []
        for var in fmu_variables:
            name = var["name"]
            causality = var.get("causality", "input")
            direction = "input" if causality == "input" else "output"
            sig = SignalDefinition.objects.create(
                workflow_step=step,
                contract_key=name,
                native_name=name,
                direction=direction,
                origin_kind=SignalOriginKind.FMU,
                data_type="number",
            )
            sigs.append(sig)
        return sigs

    def _cel_form(self, *, expression, fmu_variables):
        sigs = self._make_fmu_signals(fmu_variables)
        return RulesetAssertionForm(
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "severity": Severity.ERROR,
                "cel_expression": expression,
                "when_expression": "",
            },
            catalog_entries=sigs,
            validator=self.validator,
        )

    def test_namespaced_fmu_names_accepted(self):
        """Namespace-prefixed FMU variable names are valid CEL identifiers.

        FMU variable names must use the ``s.`` prefix for signals.
        Both input and output variable names should be accepted when
        properly namespaced.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            expression="s.T_room < s.Q_cooling_max",
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_output_prefix_accepted(self):
        """``output.T_room`` is a valid CEL identifier for an FMU output.

        The ``output.`` prefix allows explicit disambiguation in CEL
        expressions, even when there's no name collision.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "T_room", "causality": "output"},
            ],
            expression="output.T_room < 300",
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_bare_identifier_rejected(self):
        """Bare (un-namespaced) identifiers are rejected.

        Even when a bare identifier matches a known FMU variable name,
        the form requires namespace prefixes.  This ensures users get
        clear feedback directing them to use ``s.`` or ``p.`` prefixes.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "T_room", "causality": "output"},
            ],
            expression="s.T_room < unknown_var",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Bare identifiers are not allowed", str(form.errors))

    def test_mixed_signal_and_output_prefixed_identifiers(self):
        """CEL expressions can use ``s.`` for signals and ``output.`` for outputs.

        This is the typical pattern for assertions that compare an output
        signal against a user-provided input value, e.g.,
        ``s.Q_cooling_actual < s.Q_cooling_max * 0.85``.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "Q_cooling_actual", "causality": "output"},
            ],
            expression="s.Q_cooling_actual < s.Q_cooling_max",
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_bare_fmu_name_rejected(self):
        """A bare FMU variable name (without namespace prefix) is rejected.

        Even though ``Q_cooling_max`` is a known FMU input variable, the
        CEL namespace convention requires it to be referenced as ``s.Q_cooling_max``.
        Bare multi-character identifiers that aren't CEL builtins are rejected.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            expression="Q_cooling_max > 0",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Bare identifiers are not allowed", str(form.errors))
