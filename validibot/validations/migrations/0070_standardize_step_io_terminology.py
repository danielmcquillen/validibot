"""Standardize step I/O table, column, constraint, and model metadata names."""

import django.db.models.deletion
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0069_step_io_file_port_contract"),
        ("workflows", "0032_split_step_config_display_settings"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="stepiodefinition",
            name="ck_sigdef_one_owner",
        ),
        migrations.RemoveConstraint(
            model_name="stepiodefinition",
            name="uq_sigdef_validator_key_dir",
        ),
        migrations.RemoveConstraint(
            model_name="stepiodefinition",
            name="uq_sigdef_step_key_dir",
        ),
        migrations.RemoveConstraint(
            model_name="stepinputbinding",
            name="uq_binding_step_signal",
        ),
        migrations.RemoveConstraint(
            model_name="workflowstepiopromotion",
            name="uq_iopromotion_step_signal",
        ),
        migrations.RemoveConstraint(
            model_name="workflowstepiopromotion",
            name="uq_iopromotion_step_name",
        ),
        migrations.RemoveConstraint(
            model_name="rulesetassertion",
            name="ck_ruleset_assertion_target_oneof",
        ),
        migrations.RenameField(
            model_name="rulesetassertion",
            old_name="target_signal_definition",
            new_name="target_io_definition",
        ),
        migrations.RenameField(
            model_name="stepinputbinding",
            old_name="signal_definition",
            new_name="io_definition",
        ),
        migrations.RenameField(
            model_name="workflowstepiopromotion",
            old_name="signal_definition",
            new_name="io_definition",
        ),
        migrations.RenameField(
            model_name="resolvedinputtrace",
            old_name="signal_definition",
            new_name="io_definition",
        ),
        migrations.RenameField(
            model_name="resolvedinputtrace",
            old_name="signal_contract_key",
            new_name="input_contract_key",
        ),
        migrations.AlterField(
            model_name="rulesetassertion",
            name="target_io_definition",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Reference to a step I/O definition when targeting a known "
                    "input or output."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="ruleset_assertions",
                to="validations.stepiodefinition",
            ),
        ),
        migrations.AddConstraint(
            model_name="rulesetassertion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("target_data_path", ""),
                        ("target_io_definition__isnull", False),
                    ),
                    ("target_io_definition__isnull", True),
                    _connector="OR",
                ),
                name="ck_ruleset_assertion_target_oneof",
            ),
        ),
        migrations.AlterField(
            model_name="derivation",
            name="expression",
            field=models.TextField(
                help_text=(
                    "CEL expression that computes this derivation's value. Can "
                    "reference step I/O contract keys and other derivation keys."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="resolvedinputtrace",
            name="error_message",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Error message if resolution failed (e.g., required step "
                    "input not found and no default configured)."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="resolvedinputtrace",
            name="resolved",
            field=models.BooleanField(
                help_text=(
                    "Whether the resolution engine found a value for this step "
                    "input."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="resolvedinputtrace",
            name="input_contract_key",
            field=models.CharField(
                help_text=(
                    "Denormalized contract_key from the step I/O definition, "
                    "preserved for auditability if the definition is deleted."
                ),
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name="resolvedinputtrace",
            name="io_definition",
            field=models.ForeignKey(
                help_text=(
                    "The step I/O definition that was being resolved. SET_NULL "
                    "on delete so traces survive schema evolution."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="validations.stepiodefinition",
            ),
        ),
        migrations.AlterField(
            model_name="stepiodefinition",
            name="data_type",
            field=models.CharField(
                choices=[
                    ("number", "Number"),
                    ("timeseries", "Timeseries"),
                    ("string", "String"),
                    ("boolean", "Boolean"),
                    ("object", "Object"),
                    ("artifact_ref", "Artifact reference"),
                ],
                default="number",
                help_text="The data type of the step input or output value.",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="stepiodefinition",
            name="description",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Detailed description of this step input or output.",
            ),
        ),
        migrations.AlterField(
            model_name="stepiodefinition",
            name="direction",
            field=models.CharField(
                choices=[("input", "Input"), ("output", "Output")],
                help_text=(
                    "Whether this definition is consumed (input) or produced "
                    "(output)."
                ),
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="stepiodefinition",
            name="is_hidden",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "If True, this definition is hidden from the default step "
                    "I/O UI."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="stepiodefinition",
            name="is_path_editable",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Whether the workflow author can edit the source data path "
                    "for this step input's binding. False when the validator "
                    "controls the extraction path internally."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="stepiodefinition",
            name="label",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Human-readable display label for this step input or output.",
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name="stepiodefinition",
            name="native_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "The provider's original name for this step input or output, "
                    "preserved verbatim (e.g., an FMU variable name or template "
                    "placeholder)."
                ),
                max_length=500,
            ),
        ),
        migrations.AlterField(
            model_name="stepiodefinition",
            name="order",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Display ordering within the owner's step I/O list.",
            ),
        ),
        migrations.AlterField(
            model_name="stepiodefinition",
            name="origin_kind",
            field=models.CharField(
                choices=[
                    ("catalog", "Catalog"),
                    ("fmu", "FMU"),
                    ("template", "Template"),
                ],
                help_text=(
                    "How this step I/O definition was created: from a validator "
                    "config declaration, an FMU probe, or a template scan."
                ),
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="stepiodefinition",
            name="source_kind",
            field=models.CharField(
                choices=[("payload_path", "Payload Path"), ("internal", "Internal")],
                default="payload_path",
                help_text=(
                    "How this step input/output value is obtained: from a known "
                    "data path in the submission payload (PAYLOAD_PATH) or via "
                    "the validator's own internal extraction mechanism "
                    "(INTERNAL)."
                ),
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="stepiodefinition",
            name="validator",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "The validator that owns this step I/O definition. Mutually "
                    "exclusive with workflow_step."
                ),
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="step_io_definitions",
                to="validations.validator",
            ),
        ),
        migrations.AlterField(
            model_name="stepiodefinition",
            name="workflow_step",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "The workflow step that owns this step I/O definition. "
                    "Mutually exclusive with validator."
                ),
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="step_io_definitions",
                to="workflows.workflowstep",
            ),
        ),
        migrations.AlterField(
            model_name="validator",
            name="allow_custom_assertion_targets",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Allow assertions against data paths not declared by step "
                    "I/O definitions."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="validator",
            name="has_processor",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True when the validator includes an intermediate processor "
                    "that produces step outputs."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="validator",
            name="processor_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "The name of the process that generates step outputs from "
                    "step inputs."
                ),
                max_length=200,
            ),
        ),
        migrations.AddConstraint(
            model_name="stepiodefinition",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("validator__isnull", False), ("workflow_step__isnull", True)
                    ),
                    models.Q(
                        ("validator__isnull", True), ("workflow_step__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="ck_step_io_definition_one_owner",
            ),
        ),
        migrations.AddConstraint(
            model_name="stepiodefinition",
            constraint=models.UniqueConstraint(
                condition=models.Q(("validator__isnull", False)),
                fields=("validator", "contract_key", "direction"),
                name="uq_step_io_definition_validator_key_dir",
            ),
        ),
        migrations.AddConstraint(
            model_name="stepiodefinition",
            constraint=models.UniqueConstraint(
                condition=models.Q(("workflow_step__isnull", False)),
                fields=("workflow_step", "contract_key", "direction"),
                name="uq_step_io_definition_step_key_dir",
            ),
        ),
        migrations.AddConstraint(
            model_name="stepiodefinition",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("io_medium", "value"),
                    ("promoted_signal_name", ""),
                    _connector="OR",
                ),
                name="ck_step_io_promotion_value_only",
            ),
        ),
        migrations.AlterField(
            model_name="stepinputbinding",
            name="workflow_step",
            field=models.ForeignKey(
                help_text="The workflow step this binding belongs to.",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="input_bindings",
                to="workflows.workflowstep",
            ),
        ),
        migrations.AlterField(
            model_name="stepinputbinding",
            name="io_definition",
            field=models.ForeignKey(
                help_text="The step input definition this binding wires up.",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="input_bindings",
                to="validations.stepiodefinition",
            ),
        ),
        migrations.AddConstraint(
            model_name="stepinputbinding",
            constraint=models.UniqueConstraint(
                fields=("workflow_step", "io_definition"),
                name="uq_step_input_binding_definition",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowstepiopromotion",
            constraint=models.UniqueConstraint(
                fields=("workflow_step", "io_definition"),
                name="uq_step_io_promotion_definition",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowstepiopromotion",
            constraint=models.UniqueConstraint(
                fields=("workflow_step", "promoted_signal_name"),
                name="uq_step_io_promotion_name",
            ),
        ),
        migrations.AlterModelTable(
            name="stepinputbinding",
            table=None,
        ),
        migrations.AlterModelTable(
            name="stepiodefinition",
            table=None,
        ),
    ]
