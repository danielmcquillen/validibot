"""
Tests for RulesetAssertionForm — signal targeting, CEL identifier validation,
and FMU variable collision detection.

The assertion form handles multiple signal sources: catalog entries (from
the validator's declared catalog) and step-level FMU variables (discovered
from the FMU model metadata).  Both sources participate in target resolution
for basic assertions and identifier validation for CEL expressions.

The ``output.`` prefix convention lets users disambiguate output signals
from input signals that share the same name.  These tests verify that the
form enforces this convention correctly.
"""

from __future__ import annotations

from django.test import TestCase

from validibot.validations.constants import AssertionType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.forms import RulesetAssertionForm
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.utils import update_custom_validator


class RulesetAssertionFormTests(TestCase):
    """Tests for catalog-entry-backed assertions and CEL identifier validation."""

    def _form(self, *, validator, catalog_entries, data: dict, fmu_variables=None):
        return RulesetAssertionForm(
            data=data,
            catalog_entries=catalog_entries,
            validator=validator,
            fmu_variables=fmu_variables,
        )

    def test_cel_disallows_unknown_identifiers_when_custom_targets_disabled(self):
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        self.assertFalse(validator.allow_custom_assertion_targets)
        entry = ValidatorCatalogEntryFactory(validator=validator, slug="price")
        RulesetFactory()
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": entry.slug,
                "severity": Severity.ERROR,
                "cel_expression": "price > 0 && rating > 10",
                "when_expression": "",
            },
        )
        self.assertFalse(form._validator_allows_custom_targets())
        self.assertFalse(form.is_valid())
        self.assertIn("Unknown signal(s) referenced", str(form.errors))

    def test_cel_allows_unknown_identifiers_when_custom_targets_enabled(self):
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        self.assertTrue(validator.allow_custom_assertion_targets)
        entry = ValidatorCatalogEntryFactory(validator=validator, slug="price")
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": entry.slug,
                "severity": Severity.ERROR,
                "cel_expression": "price > 0 && rating > 10",
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
        input_entry = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="temperature",
            run_stage=CatalogRunStage.INPUT,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "temperature",
                "severity": Severity.ERROR,
                "cel_expression": "temperature > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid())
        self.assertIsNone(form.cleaned_data["target_catalog_entry"])

    def test_output_requires_prefix_on_collision(self):
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC, is_system=False
        )
        input_entry = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="price",
            run_stage=CatalogRunStage.INPUT,
        )
        output_entry = ValidatorCatalogEntryFactory.build(
            validator=validator,
            slug="price",
            run_stage=CatalogRunStage.OUTPUT,
        )

        form = self._form(
            validator=validator,
            catalog_entries=[input_entry, output_entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "price",
                "severity": Severity.ERROR,
                "cel_expression": "price > 0",
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
        self.assertIsNone(form_prefixed.cleaned_data["target_catalog_entry"])


# ==============================================================================
# FMU variable form validation
#
# Step-level FMU uploads store variable metadata (name, causality) in
# step.config["fmu_variables"] rather than in catalog entries.  The assertion
# form must accept these variables as valid targets and enforce the ``output.``
# prefix convention for disambiguation when a name appears as both input and
# output.
# ==============================================================================


class FMUVariableTargetResolutionTests(TestCase):
    """Tests for basic-assertion target resolution with FMU variables.

    FMU variables are passed to the form as ``fmu_variables`` — a list of
    dicts with ``name`` and ``causality`` keys.  Unlike catalog entries,
    they don't have database rows; the form stores them as
    ``target_data_path`` values.
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

    def _fmu_form(self, *, data, fmu_variables):
        """Create a form with FMU variables and no catalog entries."""
        return RulesetAssertionForm(
            data=data,
            catalog_entries=[],
            validator=self.validator,
            fmu_variables=fmu_variables,
        )

    def test_bare_fmu_input_accepted(self):
        """A bare name matching only an FMU input variable is accepted.

        When there's no collision with an output of the same name, the
        user can reference the input with its plain name.
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
        self.assertEqual(form.cleaned_data["target_data_path_value"], "Q_cooling_max")

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
        self.assertEqual(form.cleaned_data["target_data_path_value"], "T_room")

    def test_output_prefix_resolves_fmu_output(self):
        """``output.T_room`` resolves to the output FMU variable.

        The ``output.`` prefix stores the path as ``output.T_room`` so
        that _resolve_path() navigates the nested ``output`` namespace
        at evaluation time.
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
        self.assertEqual(form.cleaned_data["target_data_path_value"], "output.T_room")

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
        """``output.T_room`` resolves correctly even when the name
        collides with an input variable.
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
        self.assertEqual(form.cleaned_data["target_data_path_value"], "output.T_room")


class FMUVariableCelIdentifierTests(TestCase):
    """Tests for CEL identifier validation with FMU variables.

    When ``allow_custom_assertion_targets`` is False, the form validates
    that all identifiers in a CEL expression correspond to known signals.
    FMU variable names (both bare and ``output.``-prefixed) must be
    recognised as valid identifiers.
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

    def _cel_form(self, *, expression, fmu_variables):
        return RulesetAssertionForm(
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "severity": Severity.ERROR,
                "cel_expression": expression,
                "when_expression": "",
            },
            catalog_entries=[],
            validator=self.validator,
            fmu_variables=fmu_variables,
        )

    def test_bare_fmu_names_accepted(self):
        """Bare FMU variable names are valid CEL identifiers.

        Both input and output variable names should be accepted without
        requiring the ``output.`` prefix when there's no ambiguity.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            expression="T_room < Q_cooling_max",
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

    def test_unknown_identifier_rejected(self):
        """Identifiers not matching any FMU variable are rejected.

        This ensures users get clear feedback when they misspell a
        variable name or reference a signal that doesn't exist.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "T_room", "causality": "output"},
            ],
            expression="T_room < unknown_var",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Unknown signal(s) referenced", str(form.errors))

    def test_mixed_bare_and_prefixed_identifiers(self):
        """CEL expressions can mix bare input names with prefixed outputs.

        This is the typical pattern for assertions that compare an output
        signal against a user-provided input value, e.g.,
        ``Q_cooling_actual < Q_cooling_max * 0.85``.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "Q_cooling_actual", "causality": "output"},
            ],
            expression="Q_cooling_actual < Q_cooling_max",
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_output_prefix_for_invalid_output_rejected(self):
        """``output.Q_cooling_max`` is rejected when Q_cooling_max is
        only an input variable.

        The ``output.`` prefix namespace only contains output signals.
        Using it on an input-only variable is a user error.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            expression="output.Q_cooling_max > 0",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Unknown signal(s) referenced", str(form.errors))
