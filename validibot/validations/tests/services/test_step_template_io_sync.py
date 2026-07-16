"""
Tests for EnergyPlus template variable I/O definition synchronization.

When an author uploads an IDF template to a workflow step, the scanner
extracts ``$VARIABLE_NAME`` placeholders. These are passed to
``sync_step_template_io_definitions()`` which persists them as
``StepIODefinition`` and ``StepInputBinding`` rows.

Template variables are always step inputs (``direction=INPUT``,
``origin_kind=TEMPLATE``) — they represent values the submitter provides
to parameterize the IDF before simulation.
"""

from __future__ import annotations

from django.test import TestCase

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.models import StepInputBinding
from validibot.validations.models import StepIODefinition
from validibot.validations.services.template_step_io import (
    clear_step_template_io_definitions,
)
from validibot.validations.services.template_step_io import (
    sync_step_template_io_definitions,
)
from validibot.workflows.tests.factories import WorkflowStepFactory


def _make_template_var(
    name: str,
    description: str = "",
    default: str | float | None = None,
    units: str = "",
    variable_type: str = "text",
    **kwargs,
) -> dict:
    """Build a minimal template variable dict for testing.

    These dicts mirror the structure returned by the IDF scanner and
    are passed to ``sync_step_template_io_definitions()`` to create
    ``StepIODefinition`` rows.
    """
    return {
        "name": name,
        "description": description,
        "default": default,
        "units": units,
        "variable_type": variable_type,
        "min_value": kwargs.get("min_value"),
        "max_value": kwargs.get("max_value"),
        "min_exclusive": kwargs.get("min_exclusive", False),
        "max_exclusive": kwargs.get("max_exclusive", False),
        "choices": kwargs.get("choices", []),
    }


class SyncStepTemplateIODefinitionsTests(TestCase):
    """Tests for the sync_step_template_io_definitions() service function."""

    def test_creates_input_step_io_definitions(self):
        """Each template variable should create a StepIODefinition with
        direction=INPUT and origin_kind=TEMPLATE, owned by the step.
        """
        step = WorkflowStepFactory()
        variables = [
            _make_template_var("U_FACTOR", description="Wall U-Factor"),
            _make_template_var("HEATING_SETPOINT"),
        ]

        sync_step_template_io_definitions(step, variables)

        io_definitions = StepIODefinition.objects.filter(workflow_step=step)
        self.assertEqual(io_definitions.count(), 2)

        for io_definition in io_definitions:
            self.assertEqual(io_definition.direction, StepIODirection.INPUT)
            self.assertEqual(io_definition.origin_kind, StepIOOriginKind.TEMPLATE)
            self.assertIsNone(io_definition.validator)

    def test_creates_bindings_with_default_values(self):
        """Template variables with defaults should create StepInputBinding
        rows with default_value set. Variables without defaults should be
        marked as required (is_required=True).
        """
        step = WorkflowStepFactory()
        variables = [
            _make_template_var("U_FACTOR", default=0.35),
            _make_template_var("ZONE_NAME"),  # no default → required
        ]

        sync_step_template_io_definitions(step, variables)

        bindings = StepInputBinding.objects.filter(
            workflow_step=step,
        ).select_related("io_definition")

        self.assertEqual(bindings.count(), 2)

        u_factor_binding = bindings.get(
            io_definition__contract_key="u_factor",
        )
        self.assertEqual(u_factor_binding.default_value, 0.35)
        self.assertFalse(u_factor_binding.is_required)
        self.assertEqual(
            u_factor_binding.source_scope,
            BindingSourceScope.SUBMISSION_PAYLOAD,
        )

        zone_binding = bindings.get(
            io_definition__contract_key="zone_name",
        )
        self.assertIsNone(zone_binding.default_value)
        self.assertTrue(zone_binding.is_required)

    def test_metadata_captures_validation_constraints(self):
        """Template variable metadata should include type, min/max,
        and choices — needed by the merge/validate pipeline to enforce
        constraints before substitution.
        """
        step = WorkflowStepFactory()
        sync_step_template_io_definitions(
            step,
            [
                _make_template_var(
                    "GLAZING_U",
                    variable_type="number",
                    min_value=0.1,
                    max_value=5.0,
                    units="W/m2-K",
                ),
            ],
        )

        io_definition = StepIODefinition.objects.get(workflow_step=step)
        self.assertEqual(io_definition.unit, "W/m2-K")
        self.assertEqual(io_definition.metadata["variable_type"], "number")
        self.assertEqual(io_definition.metadata["min_value"], 0.1)
        self.assertEqual(io_definition.metadata["max_value"], 5.0)

    def test_choice_variable_type(self):
        """Choice-type template variables should store allowed values in
        metadata and map to STRING data_type.
        """
        step = WorkflowStepFactory()
        sync_step_template_io_definitions(
            step,
            [
                _make_template_var(
                    "SYSTEM_TYPE",
                    variable_type="choice",
                    choices=["VAV", "DOAS", "Fan Coil"],
                ),
            ],
        )

        io_definition = StepIODefinition.objects.get(workflow_step=step)
        self.assertEqual(io_definition.data_type, "string")
        self.assertEqual(io_definition.metadata["choices"], ["VAV", "DOAS", "Fan Coil"])

    def test_reupload_preserves_matching_variables(self):
        """Re-uploading a template with the same variable names should
        update existing rows, not create duplicates.
        """
        step = WorkflowStepFactory()

        sync_step_template_io_definitions(
            step,
            [
                _make_template_var("U_FACTOR", units="W/m2-K"),
            ],
        )
        first_pk = StepIODefinition.objects.get(workflow_step=step).pk

        sync_step_template_io_definitions(
            step,
            [
                _make_template_var("U_FACTOR", units="Btu/h-ft2-F"),
            ],
        )

        io_definitions = StepIODefinition.objects.filter(workflow_step=step)
        self.assertEqual(io_definitions.count(), 1)
        self.assertEqual(io_definitions.first().pk, first_pk)
        self.assertEqual(io_definitions.first().unit, "Btu/h-ft2-F")

    def test_reupload_removes_deleted_variables(self):
        """When the new template has different variables, I/O definitions from
        the old template should be deleted.
        """
        step = WorkflowStepFactory()

        sync_step_template_io_definitions(
            step,
            [
                _make_template_var("OLD_VAR"),
                _make_template_var("KEPT_VAR"),
            ],
        )
        self.assertEqual(
            StepIODefinition.objects.filter(workflow_step=step).count(),
            2,
        )

        sync_step_template_io_definitions(
            step,
            [
                _make_template_var("KEPT_VAR"),
            ],
        )

        io_definitions = StepIODefinition.objects.filter(workflow_step=step)
        self.assertEqual(io_definitions.count(), 1)
        self.assertEqual(io_definitions.first().contract_key, "kept_var")

    def test_clear_removes_all_template_step_io(self):
        """Clearing should remove all template-origin I/O definitions and their
        bindings. Called when the author switches to direct IDF mode.
        """
        step = WorkflowStepFactory()
        sync_step_template_io_definitions(
            step,
            [
                _make_template_var("U_FACTOR"),
                _make_template_var("HEATING_SETPOINT"),
            ],
        )
        self.assertEqual(
            StepIODefinition.objects.filter(workflow_step=step).count(),
            2,
        )

        clear_step_template_io_definitions(step)

        self.assertEqual(
            StepIODefinition.objects.filter(workflow_step=step).count(),
            0,
        )
        self.assertEqual(
            StepInputBinding.objects.filter(workflow_step=step).count(),
            0,
        )

    def test_does_not_interfere_with_fmu_step_io(self):
        """Template inputs (origin_kind=TEMPLATE) and FMU I/O definitions
        (origin_kind=FMU) on the same step should coexist without
        interference. Clearing one type should not affect the other.

        After Phase 6, each step-level FMU upload seeds seven
        parser-fact StepIODefinitions in addition to per-variable
        rows (still origin_kind=FMU, source_kind=INTERNAL). The
        coexistence-and-clear invariant must hold for ALL FMU rows,
        parser facts included.
        """
        step = WorkflowStepFactory()

        # Create an FMU I/O definition on this step
        from validibot.validations.services.fmu import PARSER_FACT_KEYS
        from validibot.validations.services.fmu_step_io import (
            sync_step_fmu_io_definitions,
        )

        sync_step_fmu_io_definitions(
            step,
            [
                {
                    "name": "T_outdoor",
                    "causality": "input",
                    "variability": "continuous",
                    "value_reference": 1,
                    "value_type": "Real",
                    "unit": "K",
                    "description": "",
                    "label": "",
                },
            ],
        )
        # FMU side: 1 variable + N parser facts (Phase 6).
        fmu_count_before_template = StepIODefinition.objects.filter(
            workflow_step=step,
            origin_kind=StepIOOriginKind.FMU,
        ).count()
        self.assertEqual(fmu_count_before_template, 1 + len(PARSER_FACT_KEYS))

        # Create a template input definition
        sync_step_template_io_definitions(
            step,
            [
                _make_template_var("U_FACTOR"),
            ],
        )

        # FMU rows still all present, plus one template row
        self.assertEqual(
            StepIODefinition.objects.filter(workflow_step=step).count(),
            1 + len(PARSER_FACT_KEYS) + 1,
        )

        # Clearing template input definitions should not affect FMU I/O definitions
        clear_step_template_io_definitions(step)
        remaining = StepIODefinition.objects.filter(workflow_step=step)
        self.assertEqual(remaining.count(), 1 + len(PARSER_FACT_KEYS))
        self.assertTrue(
            all(
                io_definition.origin_kind == StepIOOriginKind.FMU
                for io_definition in remaining
            ),
        )
