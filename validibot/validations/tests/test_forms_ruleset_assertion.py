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
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.utils import update_custom_validator


class RulesetAssertionFormTests(TestCase):
    """Tests for catalog-entry-backed assertions and CEL identifier validation."""

    def _form(
        self,
        *,
        validator,
        catalog_entries,
        data: dict,
        fmu_variables=None,
        workflow_signal_names=None,
    ):
        """Build an assertion form with signal definitions."""
        return RulesetAssertionForm(
            data=data,
            catalog_entries=catalog_entries or [],
            validator=validator,
            fmu_variables=fmu_variables,
            workflow_signal_names=workflow_signal_names,
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
        entry = StepIODefinitionFactory(validator=validator, contract_key="price")
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
        entry = StepIODefinitionFactory(validator=validator, contract_key="price")
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

    def test_basic_assertion_accepts_parser_managed_input_target(self):
        """BASIC + i.<parser_input> is accepted post Phase 5.

        Previously the form rejected this because the BASIC evaluator
        walked the raw payload by contract_key, ignoring parser-
        extracted facts. Phase 5 fixed the runtime trap at the
        validator base layer (``BaseValidator._enrich_basic_payload``
        merges resolved bindings + workflow signals + parser facts
        into the BASIC payload by their bare contract_key), so the
        form-side rejection is no longer needed.

        Regression test: BASIC + i.<parser_input> now saves cleanly
        and resolves to a StepIODefinition target the evaluator can
        walk against the enriched payload.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )
        # Parser-managed input: source_kind=internal mimics how the
        # EnergyPlus catalog declares zone_count (the IDF parser
        # fills it, not a payload binding).
        parser_input = StepIODefinitionFactory(
            validator=validator,
            contract_key="zone_count",
            direction="input",
            source_kind="internal",
            is_path_editable=False,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[parser_input],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": f"i.{parser_input.contract_key}",
                "operator": "ge",
                "comparison_value": "1",
                "severity": Severity.ERROR,
                "cel_expression": "",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # The form resolves the target to the catalog row.
        resolved = form.cleaned_data["resolved_signal"]
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.contract_key, "zone_count")

    def test_basic_assertion_accepts_author_bound_input_target(self):
        """BASIC + i.<author_bound_input> is accepted post Phase 5.

        Previously this was rejected because the BASIC evaluator
        walked the raw payload by ``contract_key`` and ignored the
        ``StepInputBinding``'s ``source_data_path``. Phase 5 fixed
        the runtime trap at the validator base layer: the validator
        calls ``_enrich_basic_payload`` which runs
        ``_resolve_bound_input_context`` and merges the binding's
        resolved value into the payload under the bare
        ``contract_key``. BASIC's ``contract_key`` lookup now hits
        the merged value directly — no payload-walk indirection.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        bound_input = StepIODefinitionFactory(
            validator=validator,
            contract_key="temperature",
            direction="input",
            source_kind="payload_path",
            is_path_editable=True,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[bound_input],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": f"i.{bound_input.contract_key}",
                "operator": "ge",
                "comparison_value": "0",
                "severity": Severity.ERROR,
                "cel_expression": "",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        resolved = form.cleaned_data["resolved_signal"]
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.contract_key, "temperature")

    def test_basic_assertion_accepts_workflow_signal_target(self):
        """BASIC + s.<workflow_signal> is accepted post Phase 5.

        Previously this was rejected because the BASIC evaluator
        walked the raw payload, ignoring ``workflow_signals``.
        Phase 5 fixed the runtime trap: the validator's
        ``_enrich_basic_payload`` helper merges
        ``run_context.workflow_signals`` into the payload by their
        bare name before evaluation. The BASIC evaluator's lookup
        for ``site_area`` now finds the workflow signal's resolved
        value at the payload root.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[],
            workflow_signal_names={"site_area"},
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "s.site_area",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
                "cel_expression": "",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # No StepIODefinition for workflow signals — the form
        # stores the bare name in target_data_path_value.
        self.assertEqual(
            form.cleaned_data["target_data_path_value"],
            "site_area",
        )

    def test_basic_assertion_allows_output_target(self):
        """BASIC + o.* still works — the guard is INPUT-only.

        Output targets resolve from extract_output_signals() and the
        validator output envelope, which BASIC's payload walk DOES
        handle correctly (the output dict is the payload at output
        stage). Only INPUT targets need to be redirected to CEL.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        output_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="site_eui",
            direction="output",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[output_sig],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": f"o.{output_sig.contract_key}",
                "operator": "lt",
                "comparison_value": "100",
                "severity": Severity.ERROR,
                "cel_expression": "",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

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
        """Bare-name target resolution prefers the input direction.

        The target_data_path bare-name resolution prefers the input
        signal over the output (richer metadata). The CEL expression
        uses i.* to reach the same value — which is the namespace
        that actually carries it at runtime. (Pre-May 2026 follow-up
        review this used s.temperature, which was the mental-model
        trap — runtime puts step inputs in i.*, never s.*.)
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC, is_system=False
        )
        input_entry = StepIODefinitionFactory(
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
                "cel_expression": "i.temperature > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid())
        # CEL expressions set target_catalog_entry to None — they declare
        # their own targets inside the expression text.
        self.assertIsNone(form.cleaned_data["target_catalog_entry"])

    def test_output_requires_prefix_on_collision(self):
        """When the same contract_key exists as both input and output,
        the bare-name target resolves to the input (richer metadata).

        The CEL expression in each case explicitly chooses the
        intended namespace — i.* for the input form, o.* for the
        prefixed-output form. (Pre-May 2026 follow-up review the
        first form used s.price; that was the mental-model trap
        we're now guarding against.)
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC, is_system=False
        )
        input_entry = StepIODefinitionFactory(
            validator=validator,
            contract_key="price",
            direction="input",
        )
        output_entry = StepIODefinitionFactory.build(
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
                "cel_expression": "i.price > 0",
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
# StepIODefinition rows.  The assertion form must accept these variables
# as valid targets and enforce the ``output.`` prefix convention for
# disambiguation when a name appears as both input and output.
# ==============================================================================


class FMUVariableTargetResolutionTests(TestCase):
    """Tests for basic-assertion target resolution with FMU variables.

    FMU variables are provided as step-owned StepIODefinition rows with
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
        """Create StepIODefinition objects from fmu_variables dicts."""
        from validibot.validations.constants import SignalOriginKind
        from validibot.validations.models import StepIODefinition
        from validibot.workflows.tests.factories import WorkflowStepFactory

        step = WorkflowStepFactory()
        sigs = []
        for var in fmu_variables:
            name = var["name"]
            causality = var.get("causality", "input")
            direction = "input" if causality == "input" else "output"
            sig = StepIODefinition.objects.create(
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

    def test_bare_fmu_input_rejected(self):
        """A bare name (no prefix) is rejected even for FMU inputs.

        Users must reference FMU inputs via the signal namespace
        (``s.Q_cooling_max``) because assertions target the source data
        feeding the validator, not the validator's internal parameters.
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
        self.assertFalse(form.is_valid())
        self.assertIn("for workflow signals", str(form.errors))

    def test_s_prefixed_fmu_input_accepted_for_basic(self):
        """``s.<fmu_input>`` BASIC assertions are accepted post Phase 5.

        Previously the form rejected this because the BASIC runtime
        walked ``contract_key`` against the raw payload, ignoring
        the binding that mapped ``Q_cooling_max`` to its actual
        submission path. Phase 5 fixed the runtime trap: the
        validator's ``_enrich_basic_payload`` runs
        ``_resolve_bound_input_context`` and merges the resolved
        binding value into the payload by its contract_key.

        The ``s.`` prefix here is the legacy alias for the
        FMU-input namespace (it predates the ``i.`` namespace).
        The form resolves it to the INPUT-direction
        StepIODefinition; runtime then walks ``contract_key``
        against the enriched payload and finds the merged value.
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "s.Q_cooling_max",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        resolved = form.cleaned_data["resolved_signal"]
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.contract_key, "Q_cooling_max")

    def test_bare_fmu_output_rejected(self):
        """A bare name (no prefix) is rejected even for FMU outputs.

        Users must reference FMU outputs via the output namespace
        (``o.T_room``).
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
        self.assertFalse(form.is_valid())
        self.assertIn("for workflow signals", str(form.errors))

    def test_o_prefixed_fmu_output_accepted(self):
        """``o.T_room`` resolves to the FMU output StepIODefinition.

        The ``o.`` prefix targets the validator output namespace.
        """
        form = self._fmu_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "o.T_room",
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
        """``output.T_room`` resolves to the FMU output StepIODefinition.

        The ``output.`` prefix is used for explicit disambiguation.
        With the unified signal model, this resolves to a StepIODefinition.
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

    def test_bare_collision_name_rejected(self):
        """A bare name that's both an FMU input and output is rejected
        because all targets now require a namespace prefix.

        The user must write ``o.T_room`` for the output or ``s.T_room``
        for the input signal.
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
        self.assertIn("for workflow signals", str(form.errors))

    def test_collision_resolved_with_output_prefix(self):
        """``output.T_room`` resolves to the output StepIODefinition
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
        """Create StepIODefinition objects from fmu_variables dicts.

        Converts the legacy dict format (name, causality) into
        step-owned StepIODefinition rows with origin_kind=FMU.
        """
        from validibot.validations.constants import SignalOriginKind
        from validibot.validations.models import StepIODefinition
        from validibot.workflows.tests.factories import WorkflowStepFactory

        step = WorkflowStepFactory()
        sigs = []
        for var in fmu_variables:
            name = var["name"]
            causality = var.get("causality", "input")
            direction = "input" if causality == "input" else "output"
            sig = StepIODefinition.objects.create(
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

        FMU inputs live in i.* (resolved from StepInputBindings before
        the container runs) and FMU outputs live in o.* (extracted from
        the output envelope). Both should be accepted when properly
        namespaced.

        Pre-May 2026 follow-up review: this test used ``s.Q_cooling_max``
        for an FMU input — the mental-model trap that runtime does
        NOT inject step inputs into s.*. Updated to use the correct
        namespace per ADR-2026-05-22b.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "T_room", "causality": "output"},
            ],
            expression="o.T_room < i.Q_cooling_max",
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

    def test_mixed_input_and_output_prefixed_identifiers(self):
        """CEL expressions can use ``i.`` for step inputs and ``o.`` for step outputs.

        This is the typical pattern for assertions that compare an
        output value against a user-provided input value, e.g.,
        ``o.Q_cooling_actual < i.Q_cooling_max * 0.85``.

        Pre-May 2026 follow-up review: this test used s.* for both
        sides — the trap is that runtime puts step inputs in i.*,
        not s.*, so the assertion would have read null at runtime.
        """
        form = self._cel_form(
            fmu_variables=[
                {"name": "Q_cooling_max", "causality": "input"},
                {"name": "Q_cooling_actual", "causality": "output"},
            ],
            expression="o.Q_cooling_actual < i.Q_cooling_max",
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


# ==============================================================================
# Prefix-based target resolution
#
# All assertion targets must use a namespace prefix (s., p., o.) unless
# the validator enables custom targets.  These tests verify the new
# prefix-based resolution logic.
# ==============================================================================


class PrefixBasedTargetResolutionTests(TestCase):
    """Tests for the prefix-based assertion target resolution.

    After the refactor, assertion targets must use explicit namespace
    prefixes:

    - ``s.<name>`` for workflow signals (always accepted)
    - ``p.<path>`` for payload data (always accepted)
    - ``o.<name>`` for validator outputs (resolved to StepIODefinition)
    - Bare names are rejected unless ``allow_custom_assertion_targets``
    """

    def _form(self, *, validator, catalog_entries, data, workflow_signal_names=None):
        return RulesetAssertionForm(
            data=data,
            catalog_entries=catalog_entries or [],
            validator=validator,
            workflow_signal_names=workflow_signal_names,
        )

    def test_i_prefix_resolves_known_input_via_cel(self):
        """``i.<name>`` works in CEL for any declared step input.

        Step inputs live in the i.* CEL namespace at runtime. The
        catalog declares them; the CEL identifier validator accepts
        i.<contract_key> for any known input.

        This replaces an older test that blessed s.<panel_area> via
        CEL. The May 2026 follow-up review surfaced that as a
        mental-model trap (same shape as the BASIC s.<input> trap):
        the form would resolve the target, but at runtime the value
        lives in i.*, not s.*, so the assertion silently reads null.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "i.panel_area",
                "severity": Severity.ERROR,
                "cel_expression": "i.panel_area > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_cel_rejects_s_prefix_on_known_step_input(self):
        """CEL rejects ``s.<known_input>`` and points the author at i.*.

        Why it matters: ``inputs_by_slug`` is checked before
        ``workflow_signal_names`` in ``_resolve_target_data_path``,
        and the CEL identifier validator was previously accepting
        ANY ``s.<name>`` reference as long as it looked
        namespace-prefixed. But step inputs live in i.* at runtime —
        they are never injected into s.*. Without this rejection,
        ``s.panel_area`` saves cleanly and then reads null in every
        evaluation.

        Regression test for the May 2026 P2 review finding that
        identified this as the CEL-side equivalent of the BASIC
        s.<input> trap fixed earlier.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        # No workflow_signal_names with this name — the name only
        # exists as a step input.
        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "s.panel_area",
                "severity": Severity.ERROR,
                "cel_expression": "s.panel_area > 0",
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        joined = " ".join(str(e) for e in form.errors.get("cel_expression", []))
        # The error must point the author at i.<name> as the fix.
        self.assertIn("i.panel_area", joined)
        self.assertIn("step inputs live in the i.* namespace", joined.lower())

    def test_cel_allows_s_prefix_when_name_is_both_input_and_workflow_signal(self):
        """A name that exists as BOTH a step input AND a workflow signal
        is a legitimate s.* target — the workflow signal half of the
        collision is real.

        The guard exists specifically to catch names that are ONLY
        known as step inputs (where s.<name> would silently read
        null). If the name is also a workflow signal, then s.<name>
        will resolve correctly at runtime (to the workflow signal's
        value), so the assertion is fine.

        Critically, the test must actually create the collision —
        passing the StepIODefinition into ``catalog_entries`` so
        ``inputs_by_slug`` contains ``panel_area`` AND seeding
        ``workflow_signal_names`` with the same name. Without
        ``catalog_entries=[input_sig]`` the test passed by accident:
        ``inputs_by_slug`` was empty, the guard's collision check
        never fired, and the test only proved "s.<workflow_signal>
        is allowed" — not the harder "s.<both_input_and_signal> is
        still allowed" case the guard's collision-aware branch is
        meant to handle.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            workflow_signal_names={"panel_area"},
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "s.panel_area",
                "severity": Severity.ERROR,
                "cel_expression": "s.panel_area > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # Belt-and-braces: confirm the test fixture actually
        # exercises the collision path. If inputs_by_slug doesn't
        # contain panel_area, the form would have passed for the
        # wrong reason.
        self.assertIn("panel_area", form.inputs_by_slug)
        self.assertIn("panel_area", form.workflow_signal_names)

    def test_cel_rejects_s_bracket_access_on_known_step_input(self):
        """CEL rejects ``s["<known_input>"]`` the same way as ``s.<known_input>``.

        Per the CEL spec, ``m.x`` and ``m["x"]`` are equivalent for
        maps with valid-identifier keys, so an author can express
        the same wrong reference via bracket access:
        ``s["panel_area"] > 0``. The previous guard only scanned
        the stripped expression (string literals removed first),
        which meant the bracket form bypassed the check — leaving
        the same mental-model trap through a different valid CEL
        spelling.

        A pre-strip scan over the bracket-access pattern catches
        both quote styles (``"`` and ``'``).
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": 's["panel_area"]',
                "severity": Severity.ERROR,
                "cel_expression": 's["panel_area"] > 0',
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        joined = " ".join(str(e) for e in form.errors.get("cel_expression", []))
        # Error points at i.<name> as the fix (same as dot-access path).
        self.assertIn("i.panel_area", joined)
        self.assertIn("step inputs live in the i.* namespace", joined.lower())

    def test_cel_rejects_signal_bracket_access_with_single_quotes(self):
        """The bracket-access guard catches single-quoted keys too.

        CEL accepts both quote styles, and we ship the long-form
        ``signal["name"]`` alias as well — covering both ensures
        an author can't slip the trap through by mixing quote style
        or namespace alias.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "signal['panel_area']",
                "severity": Severity.ERROR,
                "cel_expression": "signal['panel_area'] > 0",
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        joined = " ".join(str(e) for e in form.errors.get("cel_expression", []))
        self.assertIn("i.panel_area", joined)

    def test_cel_bracket_guard_skips_text_inside_string_literal(self):
        """The bracket guard must not false-positive on string contents.

        ``p.note == 's["panel_area"]'`` is a perfectly valid CEL
        expression that compares the value at ``p.note`` against the
        literal string ``s["panel_area"]``. No bracket access happens
        at runtime — the text just looks like one.

        The previous regex-based guard scanned the raw expression and
        false-positively rejected this with the step-input namespace
        error. The lexical scanner skips CEL string literals so the
        bracket match only fires on real syntax.

        Reproduction of the May 2026 P2 review finding (string-
        literal false positive).
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "p.note",
                "severity": Severity.ERROR,
                # The bracket-looking text is inside a single-quoted
                # CEL string — it's not bracket access, it's a string
                # comparison.
                "cel_expression": "p.note == 's[\"panel_area\"]'",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_cel_bracket_guard_catches_hyphenated_contract_key(self):
        """The bracket guard catches slug-shaped contract_keys with hyphens.

        ``StepIODefinition.contract_key`` is a Django ``SlugField``
        which allows ``-`` characters, so a catalog can legitimately
        contain a row keyed ``panel-area``. The previous regex used
        an identifier-shaped pattern (``[A-Za-z_][A-Za-z0-9_]*``) so
        ``s["panel-area"]`` slipped past — the same mental-model trap
        through a key the regex didn't recognize.

        The lexical scanner extracts the bracket contents verbatim
        and the form looks up the key in ``inputs_by_slug``
        directly, so any slug shape the catalog can produce is
        caught.

        Reproduction of the May 2026 P2 review finding (hyphenated
        key bypass).
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel-area",
            direction="input",
        )
        # Sanity: the SlugField really stored the hyphen.
        self.assertEqual(input_sig.contract_key, "panel-area")

        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                # The dot-access form is rejected by CEL syntax
                # itself (``-`` isn't a valid identifier char), so
                # bracket access is the only spelling that reaches
                # this catalog row. We're testing the guard
                # specifically for the case where the runtime
                # spelling is forced into bracket syntax.
                "target_data_path": 's["panel-area"]',
                "severity": Severity.ERROR,
                "cel_expression": 's["panel-area"] > 0',
                "when_expression": "",
            },
        )
        self.assertFalse(form.is_valid())
        joined = " ".join(str(e) for e in form.errors.get("cel_expression", []))
        # Error points the author at i.<name> (using the same
        # hyphenated key — the i.* namespace handles the same
        # contract_keys the catalog declares).
        self.assertIn("panel-area", joined)
        self.assertIn("step inputs live in the i.* namespace", joined.lower())

    def test_cel_bracket_guard_skips_member_access_p_s(self):
        """``p.s["panel_area"]`` is payload member access, not the s.* namespace.

        CEL allows arbitrary nesting: ``p`` is the payload root, and
        ``p.s`` selects a field named ``s`` on the payload. The
        subsequent ``["panel_area"]`` is then bracket access on that
        field's value (probably a map). None of that touches the s.*
        CEL namespace — the s.* guard must not false-positive on it.

        The scanner enforces this by inspecting the previous non-
        whitespace character: if it's ``.``, the candidate is a
        field access on something else, not a top-level namespace
        reference.

        Reproduction of the May 2026 P2 review finding (member-
        access false positive).
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "p.s",
                "severity": Severity.ERROR,
                "cel_expression": 'p.s["panel_area"] == 1',
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_cel_bracket_guard_skips_member_access_payload_signal(self):
        """``payload.signal["panel-area"]`` is payload member access.

        Same trap as ``p.s["…"]`` but with the long-form aliases
        (``payload`` instead of ``p``, ``signal`` instead of ``s``)
        and a hyphenated key to confirm the slug-aware lookup also
        respects the member-access exclusion.

        Reproduction of the May 2026 P2 review finding (long-form
        member-access false positive).
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel-area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "payload.signal",
                "severity": Severity.ERROR,
                "cel_expression": 'payload.signal["panel-area"] == 1',
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_cel_bracket_guard_skips_member_access_with_whitespace_dot(self):
        """``p . s["panel_area"]`` (whitespace around the dot) still member access.

        CEL is tolerant of whitespace between the receiver, the
        member-access operator, and the field name. The scanner's
        member-access check walks back over whitespace to find the
        previous non-whitespace character before deciding whether
        it's a ``.``.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "p.s",
                "severity": Severity.ERROR,
                "cel_expression": 'p . s["panel_area"] == 1',
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_cel_allows_s_bracket_access_when_name_is_workflow_signal(self):
        """Bracket-access guard honours the same collision allowance.

        ``s["panel_area"]`` is legitimate when ``panel_area`` is
        a real workflow signal — runtime resolves it via the
        workflow_signals dict. The guard must mirror the dot-access
        branch's collision exception so workflow-signal references
        through bracket syntax aren't false-positives.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            workflow_signal_names={"panel_area"},
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": 's["panel_area"]',
                "severity": Severity.ERROR,
                "cel_expression": 's["panel_area"] > 0',
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_basic_s_prefix_known_input_accepted(self):
        """BASIC + ``s.<known_input>`` is accepted post Phase 5.

        Previously the form rejected this because the BASIC
        runtime would walk ``contract_key`` against the raw payload
        and miss any binding indirection. Phase 5 wired
        ``_enrich_basic_payload`` into the validator base layer, so
        the resolved binding value is now merged into the payload
        under the bare ``contract_key`` before evaluation.

        The ``s.`` prefix is a legacy alias for INPUT-direction
        targets (predates the ``i.`` namespace). The form resolves
        it to the StepIODefinition; runtime walks the merged
        payload and finds the value.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "s.panel_area",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        resolved = form.cleaned_data["resolved_signal"]
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.contract_key, "panel_area")

    def test_s_prefix_unknown_input_rejected(self):
        """``s.<name>`` is rejected when the name doesn't match any
        declared input signal and custom targets are not allowed.

        The evaluator can only resolve targets that are known signals
        or custom paths (when permitted).  An unknown ``s.`` name would
        silently fail at runtime, so the form rejects it up front.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "s.panel_area",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("target_data_path", form.errors)

    def test_s_prefix_resolves_workflow_signal_via_cel(self):
        """``s.<name>`` resolves to a workflow-level signal (signal
        mapping or promoted upstream output) when used in a CEL
        assertion.

        Workflow signals are passed to the form via
        ``workflow_signal_names`` and should always be valid CEL
        targets regardless of the ``allow_custom_assertion_targets``
        setting. This ensures autocomplete choices that include
        workflow signals are never rejected by the form's own
        validation.

        BASIC targeting of s.* is blocked separately
        (``_reject_namespaced_basic_target``) because the BASIC
        evaluator walks the raw payload, not the s.* namespace —
        so this test uses CEL, which DOES resolve s.* through
        the namespaced context.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            workflow_signal_names={"site_area", "building_height"},
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "s.site_area",
                "severity": Severity.ERROR,
                "cel_expression": "s.site_area > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_s_prefix_prefers_validator_input_over_workflow_signal(self):
        """When a name exists as both a validator input and a workflow signal,
        the validator input takes precedence in target resolution.

        This precedence rule lives in ``_resolve_target_data_path``
        — the input signal definition wins because it's a richer
        target (provides StepIODefinition metadata for evaluators
        that can use it).

        The CEL identifier validator separately allows ``s.panel_area``
        here because ``panel_area`` IS also a real workflow signal
        (in ``workflow_signal_names``) — at runtime, s.panel_area
        will resolve to the workflow signal's value. The "s.<input>
        but not workflow signal" case is rejected by a different
        guard (see ``test_cel_rejects_s_prefix_on_known_step_input``).
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        input_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="panel_area",
            direction="input",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_sig],
            workflow_signal_names={"panel_area"},
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_data_path": "s.panel_area",
                "severity": Severity.ERROR,
                "cel_expression": "s.panel_area > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # CEL assertions don't use resolved_signal at evaluation
        # time — the form clears it (see clean()). But the
        # autocomplete and CEL-identifier validation paths still
        # exercise the same resolution lookup, so the precedence
        # rule is still meaningful to test.

    def test_p_prefix_always_accepted(self):
        """``p.<path>`` targets are always accepted without requiring
        ``allow_custom_assertion_targets``.

        Payload paths reference raw submission data and are resolved
        at the input stage before the validator runs.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "p.building.floor_area",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["resolved_signal"])
        # The "p." prefix is stripped so the evaluator resolves the
        # bare path against the raw payload dict.
        self.assertEqual(
            form.cleaned_data["target_data_path_value"],
            "building.floor_area",
        )
        from validibot.validations.constants import CatalogRunStage

        self.assertEqual(
            form.cleaned_data["resolved_stage"],
            CatalogRunStage.INPUT,
        )

    def test_payload_prefix_accepted(self):
        """The long-form ``payload.<path>`` prefix is also accepted.

        This is an alias for ``p.`` and should resolve identically.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "payload.zones[0].temp",
                "operator": "lt",
                "comparison_value": "30",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # The "payload." prefix is stripped — the evaluator resolves
        # the bare path against the payload dict.
        self.assertEqual(
            form.cleaned_data["target_data_path_value"],
            "zones[0].temp",
        )

    def test_o_prefix_resolves_output_signal(self):
        """``o.<name>`` resolves to the output StepIODefinition.

        Output-prefixed targets are resolved against the validator's
        declared output signals and set the stage to OUTPUT.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        output_sig = StepIODefinitionFactory(
            validator=validator,
            contract_key="site_eui",
            direction="output",
        )
        form = self._form(
            validator=validator,
            catalog_entries=[output_sig],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "o.site_eui",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNotNone(form.cleaned_data["resolved_signal"])
        self.assertEqual(
            form.cleaned_data["resolved_signal"].contract_key,
            "site_eui",
        )
        from validibot.validations.constants import CatalogRunStage

        self.assertEqual(
            form.cleaned_data["resolved_stage"],
            CatalogRunStage.OUTPUT,
        )

    def test_bare_name_rejected_without_custom_targets(self):
        """A bare name without any namespace prefix is rejected when
        ``allow_custom_assertion_targets`` is False.

        The error message should direct the user to use ``s.``, ``p.``,
        or ``o.`` prefixes.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "temperature",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("for workflow signals", str(form.errors))

    def test_bare_name_accepted_with_custom_targets(self):
        """A bare dotted path is accepted when the validator enables
        custom assertion targets.

        This provides backward compatibility for validators that
        allow free-form data path targeting.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        form = self._form(
            validator=validator,
            catalog_entries=[],
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "metrics.custom.value",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data["target_data_path_value"],
            "metrics.custom.value",
        )

    def test_signal_prefix_long_form_accepted_for_basic(self):
        """The long-form ``signal.<name>`` prefix is accepted post Phase 5.

        Both ``s.`` and ``signal.`` route through the same
        workflow-signal resolution path. Previously both were
        rejected for BASIC because the evaluator walked the raw
        payload. Phase 5's ``_enrich_basic_payload`` merges
        workflow signals into the payload by their bare name, so
        the ``contract_key`` lookup finds the value.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[],
            workflow_signal_names={"panel_area"},
            data={
                "assertion_type": AssertionType.BASIC.value,
                "target_data_path": "signal.panel_area",
                "operator": "gt",
                "comparison_value": "0",
                "severity": Severity.ERROR,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # No StepIODefinition — workflow signal stored as bare path.
        self.assertEqual(
            form.cleaned_data["target_data_path_value"],
            "panel_area",
        )
