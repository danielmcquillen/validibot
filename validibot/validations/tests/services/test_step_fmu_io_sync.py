"""
Tests for step-level FMU I/O definition synchronization.

When a user uploads an FMU to a workflow step, the system creates
``StepIODefinition`` and ``StepInputBinding`` rows from the
introspected FMU variables, PLUS seven parser-fact INPUT rows
(model_name, fmi_version, variable counts, has_simulation_defaults)
per the Phase 6 / May-2026 P1 fix. The parser-fact rows give
step-level FMU uploads identical ``i.*`` resolution to library FMU
validators.

This test suite verifies the sync function handles all lifecycle
scenarios: initial upload, re-upload with changed variables, removal,
and edge cases like slug collisions. Most assertions filter parser
facts out via ``_variable_io_definitions`` so the tests stay focused on the
variable-level sync logic without restating the parser-fact contract
in every test.
"""

from __future__ import annotations

from django.test import TestCase

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOOriginKind
from validibot.validations.models import StepInputBinding
from validibot.validations.models import StepIODefinition
from validibot.validations.services.fmu import PARSER_FACT_KEYS
from validibot.validations.services.fmu_step_io import clear_step_fmu_io_definitions
from validibot.validations.services.fmu_step_io import sync_step_fmu_io_definitions
from validibot.workflows.tests.factories import WorkflowStepFactory


def _variable_io_definitions(step):
    """Return only variable-level FMU I/O definitions, filtering out parser facts.

    Parser facts (``model_name``, ``fmi_version``, etc.) are seeded on
    every step-level FMU sync to match the system FMU validator
    catalog. Tests that assert about variable-level sync logic should
    filter them out so per-test setup describes only the variables it
    cares about.
    """
    return StepIODefinition.objects.filter(workflow_step=step).exclude(
        contract_key__in=PARSER_FACT_KEYS,
    )


def _make_fmu_var(
    name: str,
    causality: str = "input",
    value_type: str = "Real",
    unit: str = "",
    description: str = "",
) -> dict:
    """Build a minimal FMU variable dict for testing.

    These dicts mirror the structure returned by FMU introspection and
    are passed to ``sync_step_fmu_io_definitions()`` to create
    ``StepIODefinition`` rows.
    """
    return {
        "name": name,
        "causality": causality,
        "variability": "continuous",
        "value_reference": 1,
        "value_type": value_type,
        "unit": unit,
        "description": description,
        "label": "",
    }


class SyncStepFMUIODefinitionsTests(TestCase):
    """Tests for the sync_step_fmu_io_definitions() service function."""

    def test_creates_step_io_definitions_for_inputs_and_outputs(self):
        """Input and output FMU variables should each get a StepIODefinition
        with the correct direction and step FK ownership.
        """
        step = WorkflowStepFactory()
        variables = [
            _make_fmu_var("T_outdoor", causality="input"),
            _make_fmu_var("Q_heating", causality="output"),
        ]

        sync_step_fmu_io_definitions(step, variables)

        io_definitions = _variable_io_definitions(step)
        self.assertEqual(io_definitions.count(), 2)

        input_definition = io_definitions.get(direction=StepIODirection.INPUT)
        self.assertEqual(input_definition.contract_key, "t_outdoor")
        self.assertEqual(input_definition.native_name, "T_outdoor")
        self.assertEqual(input_definition.origin_kind, StepIOOriginKind.FMU)

        output_definition = io_definitions.get(direction=StepIODirection.OUTPUT)
        self.assertEqual(output_definition.contract_key, "q_heating")
        self.assertEqual(output_definition.native_name, "Q_heating")

    def test_creates_bindings_for_input_values_only(self):
        """Input variables should get a StepInputBinding with the variable
        name as source_data_path. Output variables should not get bindings
        (they're produced, not consumed).
        """
        step = WorkflowStepFactory()
        variables = [
            _make_fmu_var("T_outdoor", causality="input"),
            _make_fmu_var("Q_heating", causality="output"),
        ]

        sync_step_fmu_io_definitions(step, variables)

        bindings = StepInputBinding.objects.filter(workflow_step=step)
        self.assertEqual(bindings.count(), 1)

        binding = bindings.first()
        self.assertEqual(binding.source_scope, BindingSourceScope.SUBMISSION_PAYLOAD)
        self.assertEqual(binding.source_data_path, "")
        self.assertTrue(binding.is_required)

    def test_reupload_preserves_author_binding_path(self):
        """Re-uploading an unchanged FMU does NOT overwrite author-mapped paths.

        ``StepInputBinding.source_data_path`` is AUTHOR STATE: the
        workflow author chooses how each FMU input gets resolved at
        runtime (e.g., mapping ``T_outdoor`` to ``weather.outdoor_temp``
        on the submission payload). The May 2026 review's P1 finding
        caught that the prior ``update_or_create(defaults={...})``
        contract silently reset that mapping back to "" on every
        re-sync of an unchanged variable.

        The fix switched to ``get_or_create``, which only applies
        defaults on creation. This test pins that behaviour by:
          1. Syncing the FMU and grabbing the auto-created binding
          2. Customising its source_data_path the way an author would
          3. Re-syncing the SAME variable
          4. Asserting both the binding PK AND the custom path survive
        """
        step = WorkflowStepFactory()
        sync_step_fmu_io_definitions(
            step,
            [_make_fmu_var("T_outdoor", causality="input")],
        )

        # Author maps the input to a real payload path.
        binding = StepInputBinding.objects.get(workflow_step=step)
        original_pk = binding.pk
        binding.source_data_path = "weather.outdoor_temp"
        binding.is_required = False  # also flip required to catch overwrites
        binding.save(update_fields=["source_data_path", "is_required"])

        # Re-sync the same FMU (no variable change).
        sync_step_fmu_io_definitions(
            step,
            [_make_fmu_var("T_outdoor", causality="input")],
        )

        # Binding must be the SAME row (identity-stable) AND retain
        # the author's mapping. A regression to update_or_create
        # would reset source_data_path to "" and is_required to True.
        binding.refresh_from_db()
        self.assertEqual(
            binding.pk,
            original_pk,
            "Binding row PK changed across re-sync (identity-stability regression).",
        )
        self.assertEqual(
            binding.source_data_path,
            "weather.outdoor_temp",
            "Re-sync clobbered the author's source_data_path — the "
            "exact May 2026 P1 binding-preservation regression.",
        )
        self.assertFalse(
            binding.is_required,
            "Re-sync flipped is_required back to the default — same "
            "class of regression as source_data_path.",
        )

    def test_skips_parameter_variables(self):
        """FMU variables with causality 'parameter' or 'local' should not
        create StepIODefinition rows — they're internal to the FMU model.
        """
        step = WorkflowStepFactory()
        variables = [
            _make_fmu_var("T_outdoor", causality="input"),
            _make_fmu_var("some_param", causality="parameter"),
            _make_fmu_var("internal_state", causality="local"),
        ]

        sync_step_fmu_io_definitions(step, variables)

        io_definitions = _variable_io_definitions(step)
        self.assertEqual(io_definitions.count(), 1)
        self.assertEqual(io_definitions.first().native_name, "T_outdoor")

    def test_reupload_preserves_matching_variables(self):
        """Re-uploading an FMU with overlapping variable names should update
        existing StepIODefinition rows, not create duplicates.
        """
        step = WorkflowStepFactory()

        # First upload
        sync_step_fmu_io_definitions(
            step,
            [
                _make_fmu_var("T_outdoor", causality="input", unit="K"),
            ],
        )
        self.assertEqual(_variable_io_definitions(step).count(), 1)
        first_definition = _variable_io_definitions(step).get()
        first_pk = first_definition.pk

        # Re-upload with same variable but different unit
        sync_step_fmu_io_definitions(
            step,
            [
                _make_fmu_var("T_outdoor", causality="input", unit="degC"),
            ],
        )

        io_definitions = _variable_io_definitions(step)
        self.assertEqual(io_definitions.count(), 1)
        updated_definition = io_definitions.first()
        # Same row, updated in place
        self.assertEqual(updated_definition.pk, first_pk)
        self.assertEqual(updated_definition.unit, "degC")

    def test_reupload_removes_deleted_variables(self):
        """When a new FMU has different variables, I/O definitions from the old FMU
        should be deleted. This is important for keeping the step I/O model
        consistent with the actual FMU.
        """
        step = WorkflowStepFactory()

        # First upload: two variables
        sync_step_fmu_io_definitions(
            step,
            [
                _make_fmu_var("T_outdoor", causality="input"),
                _make_fmu_var("Q_old_output", causality="output"),
            ],
        )
        self.assertEqual(_variable_io_definitions(step).count(), 2)

        # Re-upload: only one variable, the other is gone
        sync_step_fmu_io_definitions(
            step,
            [
                _make_fmu_var("T_outdoor", causality="input"),
            ],
        )

        io_definitions = _variable_io_definitions(step)
        self.assertEqual(io_definitions.count(), 1)
        self.assertEqual(io_definitions.first().native_name, "T_outdoor")

    def test_clear_step_fmu_io_definitions_removes_all(self):
        """Clearing I/O definitions removes all FMU definitions and bindings.

        This is called when the user removes the FMU from the step.
        """
        step = WorkflowStepFactory()
        sync_step_fmu_io_definitions(
            step,
            [
                _make_fmu_var("T_outdoor", causality="input"),
                _make_fmu_var("Q_heating", causality="output"),
            ],
        )
        self.assertEqual(_variable_io_definitions(step).count(), 2)
        # All FMU-origin rows (parser facts + variables) live before clear.
        self.assertGreater(
            StepIODefinition.objects.filter(workflow_step=step).count(),
            2,
        )

        clear_step_fmu_io_definitions(step)

        # clear_step_fmu_io_definitions filters by origin_kind=FMU, which
        # covers both variable rows and parser-fact rows.
        self.assertEqual(
            StepIODefinition.objects.filter(workflow_step=step).count(),
            0,
        )
        self.assertEqual(
            StepInputBinding.objects.filter(workflow_step=step).count(),
            0,
        )

    def test_provider_binding_and_metadata_populated(self):
        """The provider_binding JSON should capture FMU-specific properties
        (causality) and metadata should capture variability, value_reference,
        and value_type for downstream use by the resolution engine.
        """
        step = WorkflowStepFactory()
        sync_step_fmu_io_definitions(
            step,
            [
                {
                    "name": "T_zone",
                    "causality": "output",
                    "variability": "continuous",
                    "value_reference": 42,
                    "value_type": "Real",
                    "unit": "K",
                    "description": "Zone temperature",
                    "label": "Zone Temp",
                },
            ],
        )

        # Filter out parser-fact rows so .get() resolves to the single
        # variable-level I/O definition under test.
        io_definition = _variable_io_definitions(step).get()
        self.assertEqual(io_definition.provider_binding["causality"], "output")
        self.assertEqual(io_definition.metadata["value_reference"], 42)
        self.assertEqual(io_definition.metadata["variability"], "continuous")
        self.assertEqual(io_definition.unit, "K")
        self.assertEqual(io_definition.description, "Zone temperature")

    def test_data_type_mapping(self):
        """FMU value types should map to the correct I/O definition data types:
        Real/Integer → number, Boolean → boolean, String → string.
        """
        step = WorkflowStepFactory()
        sync_step_fmu_io_definitions(
            step,
            [
                _make_fmu_var("real_var", causality="input", value_type="Real"),
                _make_fmu_var("bool_var", causality="input", value_type="Boolean"),
                _make_fmu_var("str_var", causality="input", value_type="String"),
            ],
        )

        io_definitions = {
            io_definition.native_name: io_definition
            for io_definition in StepIODefinition.objects.filter(workflow_step=step)
        }
        self.assertEqual(io_definitions["real_var"].data_type, "number")
        self.assertEqual(io_definitions["bool_var"].data_type, "boolean")
        self.assertEqual(io_definitions["str_var"].data_type, "string")

    def test_renamed_variable_creates_new_io_definition(self):
        """When an FMU variable is renamed (e.g., T_outdoor → T_ambient),
        the old I/O definition is deleted and a new one is created. This means
        any assertions targeting the old contract_key will break.

        This is a known limitation deferred to Phase 6, when assertion
        targets migrate from ValidatorCatalogEntry FK to StepIODefinition
        FK. At that point, value_reference-based matching can be used to
        preserve stable contract_keys across renames.
        """
        step = WorkflowStepFactory()

        # First upload: T_outdoor
        sync_step_fmu_io_definitions(
            step,
            [
                _make_fmu_var("T_outdoor", causality="input"),
            ],
        )
        old_definition = _variable_io_definitions(step).get()
        self.assertEqual(old_definition.contract_key, "t_outdoor")

        # Re-upload: same variable renamed to T_ambient
        sync_step_fmu_io_definitions(
            step,
            [
                _make_fmu_var("T_ambient", causality="input"),
            ],
        )

        # Old I/O definition is gone, new one exists
        io_definitions = list(_variable_io_definitions(step))
        self.assertEqual(len(io_definitions), 1)
        self.assertEqual(io_definitions[0].contract_key, "t_ambient")
        self.assertNotEqual(io_definitions[0].pk, old_definition.pk)

    def test_reupload_preserves_assertion_targets_on_io_definition_deletion(self):
        """When an FMU re-upload removes a variable that has assertions
        targeting it, the assertion's target_data_path should be set to
        the old contract_key so it remains valid under the XOR constraint.
        Without this fallback, SET_NULL on the FK would leave all three
        target fields empty, violating the database constraint.
        """
        from validibot.validations.tests.factories import RulesetAssertionFactory
        from validibot.validations.tests.factories import RulesetFactory

        step = WorkflowStepFactory()

        # Create I/O definition and assertion targeting it
        sync_step_fmu_io_definitions(
            step,
            [
                _make_fmu_var("Q_old", causality="output"),
            ],
        )
        io_definition = StepIODefinition.objects.get(
            workflow_step=step,
            contract_key="q_old",
        )

        # Create a ruleset and assertion targeting this I/O definition
        ruleset = RulesetFactory()
        assertion = RulesetAssertionFactory(
            ruleset=ruleset,
            target_io_definition=io_definition,
            target_data_path="",
        )

        # Re-upload without Q_old → I/O definition gets deleted
        sync_step_fmu_io_definitions(
            step,
            [
                _make_fmu_var("Q_new", causality="output"),
            ],
        )

        # Assertion should have been migrated to target_data_path fallback
        assertion.refresh_from_db()
        self.assertIsNone(assertion.target_io_definition)
        self.assertEqual(assertion.target_data_path, "q_old")
