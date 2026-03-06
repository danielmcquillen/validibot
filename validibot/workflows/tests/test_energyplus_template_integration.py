"""
Integration tests for the EnergyPlus parameterized template workflow.

These tests verify the end-to-end pipeline from form submission through
config building and resource syncing:

1. ``build_energyplus_config()`` — Validates uploaded IDF files, scans for
   ``$VARIABLE_NAME`` placeholders, populates ``template_variables`` in the
   config dict, handles template removal, and preserves existing config when
   no upload occurs.  Also merges author annotations from the Template
   Variable Editor (Phase 3) into the stored config.

2. ``_sync_energyplus_resources()`` — Creates/deletes ``WorkflowStepResource``
   rows with ``role=MODEL_TEMPLATE`` for step-owned template files.

3. ``save_workflow_step()`` — File type enforcement ensures workflows with
   parameterized templates accept JSON submissions.

4. Template Variable Editor (Phase 3) — Dynamic form fields for per-variable
   annotation, including type constraints, defaults, min/max bounds, and
   choices.  The ``template_variable_fields`` property groups BoundField
   objects for template rendering.

5. ``launch_energyplus_validation()`` (Phase 4) — Launcher integration
   testing with template mode detection, parameter validation, and
   substitution.  These tests use real Django models with mocked GCS/Cloud
   Run I/O to verify the orchestration pipeline.

Unlike the pure-Python scanner tests in ``test_idf_template.py``, these
tests require a Django database because they exercise form objects, model
instances, and ORM queries.

Phases: 2-4 of the EnergyPlus Parameterized Templates ADR.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ENERGYPLUS_MODEL_TEMPLATE
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.factories import ValidatorResourceFileFactory
from validibot.workflows.forms import EnergyPlusStepConfigForm
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.tests.factories import WorkflowStepResourceFactory
from validibot.workflows.views_helpers import _sync_energyplus_resources
from validibot.workflows.views_helpers import build_energyplus_config
from validibot.workflows.views_helpers import save_workflow_step

pytestmark = pytest.mark.django_db

# ---------------------------------------------------------------------------
# Shared IDF template content for integration tests
# ---------------------------------------------------------------------------

VALID_TEMPLATE_IDF = b"""\
Version,
    24.2;  !- Version Identifier

WindowMaterial:SimpleGlazingSystem,
    Glazing System,          !- Name
    $U_FACTOR,               !- U-Factor {W/m2-K}
    $SHGC,                   !- Solar Heat Gain Coefficient
    $VISIBLE_TRANSMITTANCE;  !- Visible Transmittance
"""


def _make_energyplus_validator():
    """Create an EnergyPlus validator with a weather file resource.

    Also creates a default weather file resource so the form's required
    ``weather_file`` ChoiceField has at least one selectable option.
    Returns the validator instance.
    """
    validator = ValidatorFactory(
        validation_type=ValidationType.ENERGYPLUS,
        slug="energyplus-test",
        name="EnergyPlus Test",
    )
    # The form requires a weather file selection.  Create one so the
    # ChoiceField has a valid option.
    ValidatorResourceFileFactory(validator=validator, is_default=True)
    return validator


def _make_template_upload(
    content: bytes = VALID_TEMPLATE_IDF,
    filename: str = "template.idf",
) -> SimpleUploadedFile:
    """Create a SimpleUploadedFile for a template IDF."""
    return SimpleUploadedFile(filename, content, content_type="text/plain")


def _make_form(
    *,
    step=None,
    org=None,
    validator=None,
    data=None,
    files=None,
):
    """Build an EnergyPlusStepConfigForm with sensible defaults.

    The ``data`` dict must include all required form fields (``name``,
    ``weather_file``, etc.) or validation will fail.  The ``files`` dict
    can contain a ``template_file`` key for upload tests.

    Auto-selects the first available weather file from the database to
    satisfy the required ChoiceField — callers don't need to pass it.

    When a step has ``template_variables`` in its config, the
    dynamic ``tplvar_*`` fields are auto-populated from the existing
    config values.  This simulates a browser form submission where
    pre-populated fields are included in the POST data.  Callers can
    override individual fields via the ``data`` dict.
    """
    if data is None:
        data = {}

    # Auto-select a weather file from the DB if one exists and
    # the caller didn't explicitly set one.
    weather_file_id = data.get("weather_file", "")
    if not weather_file_id and validator:
        from validibot.validations.constants import ResourceFileType
        from validibot.validations.models import ValidatorResourceFile

        vrf = ValidatorResourceFile.objects.filter(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        ).first()
        if vrf:
            weather_file_id = str(vrf.pk)

    defaults = {
        "name": "Test Step",
        "weather_file": weather_file_id,
        "idf_checks": [],
        "run_simulation": False,
        "case_sensitive": True,
        "remove_template": False,
    }

    # Auto-populate tplvar_* fields from step config.  This mirrors
    # what happens in a real browser submission: the pre-populated
    # initial values are included in the POST data.
    if step:
        config = step.config or {}
        for i, var in enumerate(config.get("template_variables", [])):
            prefix = f"tplvar_{i}"
            defaults.setdefault(f"{prefix}_description", var.get("description", ""))
            defaults.setdefault(f"{prefix}_default", var.get("default", ""))
            defaults.setdefault(f"{prefix}_units", var.get("units", ""))
            defaults.setdefault(
                f"{prefix}_variable_type", var.get("variable_type", "text")
            )
            min_val = var.get("min_value")
            defaults.setdefault(
                f"{prefix}_min_value", str(min_val) if min_val is not None else ""
            )
            defaults.setdefault(
                f"{prefix}_min_exclusive", var.get("min_exclusive", False)
            )
            max_val = var.get("max_value")
            defaults.setdefault(
                f"{prefix}_max_value", str(max_val) if max_val is not None else ""
            )
            defaults.setdefault(
                f"{prefix}_max_exclusive", var.get("max_exclusive", False)
            )
            choices_list = var.get("choices", [])
            defaults.setdefault(f"{prefix}_choices", "\n".join(choices_list))

    defaults.update(data)

    form = EnergyPlusStepConfigForm(
        data=defaults,
        files=files or {},
        step=step,
        org=org,
        validator=validator,
    )
    return form


# ══════════════════════════════════════════════════════════════════════════════
# build_energyplus_config() — template upload
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildConfigWithTemplateUpload:
    """Tests for ``build_energyplus_config`` when a template file is uploaded.

    This verifies the pipeline: uploaded file → ``validate_idf_template()``
    → ``scan_idf_template_variables()`` → ``template_variables`` dicts in
    the config.  The scan/validation is tested exhaustively in
    ``test_idf_template.py``; here we test the integration with the form
    and config builder.
    """

    def test_upload_populates_template_variables(self):
        """A valid template upload produces ``template_variables`` in config.

        The config should contain one dict per detected variable, with
        auto-populated metadata from the IDF annotation.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form)

        assert "template_variables" in config
        assert len(config["template_variables"]) == 3  # noqa: PLR2004
        names = [v["name"] for v in config["template_variables"]]
        assert names == ["U_FACTOR", "SHGC", "VISIBLE_TRANSMITTANCE"]

    def test_upload_preserves_annotation_metadata(self):
        """Auto-populated descriptions and units from IDF annotations are
        stored in the variable dicts.

        The ``!- U-Factor {W/m2-K}`` annotation should produce
        ``description='U-Factor'`` and ``units='W/m2-K'``.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form)

        u_factor = config["template_variables"][0]
        assert u_factor["description"] == "U-Factor"
        assert u_factor["units"] == "W/m2-K"

    def test_upload_stores_case_sensitive_setting(self):
        """The ``case_sensitive`` form value is included in the config.

        This setting controls how the scanner detects variables and must
        be persisted so future scans use the same mode.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            data={"case_sensitive": False},
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form)

        assert config["case_sensitive"] is False

    def test_upload_initializes_display_signals_empty(self):
        """Display signals start empty — the author configures them later.

        The empty list means "show all signals" (backward-compatible default).
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form)

        assert config["display_signals"] == []

    def test_upload_invalid_file_raises_validation_error(self):
        """An invalid template file causes ``ValidationError``.

        ``build_energyplus_config()`` delegates to ``validate_idf_template()``,
        which produces blocking errors for non-IDF files.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload(
            content=b'{"not": "idf"}',
            filename="not_idf.epjson",
        )
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors

        with pytest.raises(ValidationError):
            build_energyplus_config(form)

    def test_upload_attaches_warnings_to_form(self):
        """Non-blocking warnings from the scan are attached to the form.

        The view layer can then display these to the author (e.g., "This
        template is 600 KB. Consider using ##include.").
        """
        # Use a template with a bare $ to trigger a warning
        content = b"""\
WindowMaterial:SimpleGlazingSystem,
    Glazing System,          !- Name
    $U_FACTOR,               !- U-Factor {W/m2-K}
    $ SHGC,                  !- Solar Heat Gain Coefficient
    $VISIBLE_TRANSMITTANCE;  !- Visible Transmittance
"""
        validator = _make_energyplus_validator()
        upload = _make_template_upload(content=content)
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        build_energyplus_config(form)

        assert hasattr(form, "template_warnings")
        assert len(form.template_warnings) > 0

    def test_upload_variable_defaults_are_text_type(self):
        """Auto-detected variables default to ``variable_type='text'``.

        The author refines types (number, choice) in the Template Variable
        Editor (Phase 3).  Until then, 'text' is the safest default because
        it accepts any value.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form)

        for var in config["template_variables"]:
            assert var["variable_type"] == "text"
            assert var["default"] == ""
            assert var["choices"] == []


# ══════════════════════════════════════════════════════════════════════════════
# build_energyplus_config() — template removal
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildConfigWithTemplateRemoval:
    """Tests for ``build_energyplus_config`` when the template is removed.

    Removal means the author is switching from template mode back to direct
    IDF submission.  All template metadata should be cleared from config.
    """

    def test_remove_clears_template_variables(self):
        """``remove_template=True`` clears the ``template_variables`` list.

        Even if the step had template variables before, the config should
        come back with an empty list.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": [{"name": "U_FACTOR"}],
                "case_sensitive": True,
            },
        )
        form = _make_form(
            validator=validator,
            step=step,
            data={"remove_template": True},
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step)

        assert config["template_variables"] == []
        assert config["case_sensitive"] is True
        assert config["display_signals"] == []

    def test_remove_preserves_non_template_config(self):
        """Simulation settings are preserved even when template is removed.

        ``idf_checks`` and ``run_simulation`` are independent of the
        template feature and should survive template removal.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={"idf_checks": ["duplicate-names"], "run_simulation": True},
        )
        form = _make_form(
            validator=validator,
            step=step,
            data={
                "remove_template": True,
                "idf_checks": ["duplicate-names"],
                "run_simulation": True,
            },
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step)

        assert config["idf_checks"] == ["duplicate-names"]
        assert config["run_simulation"] is True


# ══════════════════════════════════════════════════════════════════════════════
# build_energyplus_config() — no upload, no removal
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildConfigPreservesExisting:
    """Tests for ``build_energyplus_config`` when no template change occurs.

    If the author edits other settings (idf_checks, run_simulation) without
    touching the template, existing template metadata should be preserved.
    """

    def test_preserves_existing_template_variables(self):
        """Existing ``template_variables`` survive a settings-only edit.

        If the author only changes ``run_simulation``, the template metadata
        from the previous save should carry forward unchanged.  The merge
        logic reads the dynamic ``tplvar_*`` form fields, which are auto-
        populated from step config by ``_make_form``.
        """
        validator = _make_energyplus_validator()
        existing_vars = [
            {
                "name": "U_FACTOR",
                "description": "U-Factor",
                "default": "",
                "units": "W/m2-K",
                "variable_type": "text",
                "min_value": None,
                "min_exclusive": False,
                "max_value": None,
                "max_exclusive": False,
                "choices": [],
            },
            {
                "name": "SHGC",
                "description": "Solar Heat Gain Coefficient",
                "default": "",
                "units": "",
                "variable_type": "text",
                "min_value": None,
                "min_exclusive": False,
                "max_value": None,
                "max_exclusive": False,
                "choices": [],
            },
        ]
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": existing_vars,
                "case_sensitive": True,
            },
        )
        form = _make_form(
            validator=validator,
            step=step,
            data={"run_simulation": True},
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step)

        assert len(config["template_variables"]) == 2  # noqa: PLR2004
        assert config["template_variables"][0]["name"] == "U_FACTOR"
        assert config["template_variables"][0]["description"] == "U-Factor"
        assert config["template_variables"][0]["units"] == "W/m2-K"
        assert config["template_variables"][1]["name"] == "SHGC"
        assert config["case_sensitive"] is True

    def test_no_step_no_template_keys(self):
        """A new step (no existing step) has no template metadata in config.

        When ``step=None`` and no template is uploaded, the config should
        only contain simulation settings — no ``template_variables`` key.
        """
        validator = _make_energyplus_validator()
        form = _make_form(validator=validator)
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step=None)

        assert "template_variables" not in config

    def test_step_without_template_has_no_template_keys(self):
        """An existing step that never had a template has no template keys.

        This is the backward-compatibility path: pre-template steps have
        ``config={"idf_checks": [], "run_simulation": False}`` with no
        template keys.  Re-saving should preserve this state.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={"idf_checks": [], "run_simulation": False},
        )
        form = _make_form(validator=validator, step=step)
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step)

        assert "template_variables" not in config


# ══════════════════════════════════════════════════════════════════════════════
# _sync_energyplus_resources() — MODEL_TEMPLATE resource management
# ══════════════════════════════════════════════════════════════════════════════


class TestSyncResourcesTemplate:
    """Tests for ``_sync_energyplus_resources()`` MODEL_TEMPLATE handling.

    The template file is stored as a step-owned ``WorkflowStepResource``
    (mode 2: ``step_resource_file`` populated, ``validator_resource_file``
    NULL).  These tests verify create, replace, and delete operations.
    """

    def test_upload_creates_step_owned_resource(self):
        """Uploading a template creates a ``WorkflowStepResource`` with
        the template file stored directly on the record.

        The resource should use ``role=MODEL_TEMPLATE`` and store the
        original filename and resource type.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)
        upload = _make_template_upload()

        form = _make_form(
            validator=validator,
            step=step,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors

        _sync_energyplus_resources(step, form)

        resource = step.step_resources.filter(
            role=WorkflowStepResource.MODEL_TEMPLATE,
        ).first()
        assert resource is not None
        assert resource.filename == "template.idf"
        assert resource.resource_type == ENERGYPLUS_MODEL_TEMPLATE
        assert resource.validator_resource_file is None  # step-owned, not catalog
        assert resource.step_resource_file  # file field is populated

    def test_upload_replaces_existing_template(self):
        """A new upload replaces any existing template resource.

        The old resource row (and its file) should be deleted before
        creating the new one.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)

        # Create initial template resource
        first_upload = _make_template_upload(filename="first.idf")
        form1 = _make_form(
            validator=validator,
            step=step,
            files={"template_file": first_upload},
        )
        assert form1.is_valid(), form1.errors
        _sync_energyplus_resources(step, form1)

        assert (
            step.step_resources.filter(
                role=WorkflowStepResource.MODEL_TEMPLATE,
            ).count()
            == 1
        )

        # Upload replacement template
        second_upload = _make_template_upload(filename="second.idf")
        form2 = _make_form(
            validator=validator,
            step=step,
            files={"template_file": second_upload},
        )
        assert form2.is_valid(), form2.errors
        _sync_energyplus_resources(step, form2)

        # Should still be exactly one template resource
        resources = step.step_resources.filter(
            role=WorkflowStepResource.MODEL_TEMPLATE,
        )
        assert resources.count() == 1
        assert resources.first().filename == "second.idf"

    def test_remove_deletes_template_resource(self):
        """``remove_template=True`` deletes the template resource row."""
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)

        # Create template resource
        upload = _make_template_upload()
        form1 = _make_form(
            validator=validator,
            step=step,
            files={"template_file": upload},
        )
        assert form1.is_valid(), form1.errors
        _sync_energyplus_resources(step, form1)

        assert (
            step.step_resources.filter(
                role=WorkflowStepResource.MODEL_TEMPLATE,
            ).count()
            == 1
        )

        # Remove template
        form2 = _make_form(
            validator=validator,
            step=step,
            data={"remove_template": True},
        )
        assert form2.is_valid(), form2.errors
        _sync_energyplus_resources(step, form2)

        assert (
            step.step_resources.filter(
                role=WorkflowStepResource.MODEL_TEMPLATE,
            ).count()
            == 0
        )

    def test_no_upload_preserves_existing_resource(self):
        """When no file is uploaded and no removal, existing template
        resource is untouched.

        This is the normal case when the author edits simulation settings
        without touching the template.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)

        # Create template resource
        upload = _make_template_upload()
        form1 = _make_form(
            validator=validator,
            step=step,
            files={"template_file": upload},
        )
        assert form1.is_valid(), form1.errors
        _sync_energyplus_resources(step, form1)

        resource_pk = (
            step.step_resources.filter(
                role=WorkflowStepResource.MODEL_TEMPLATE,
            )
            .first()
            .pk
        )

        # Submit form without template changes
        form2 = _make_form(validator=validator, step=step)
        assert form2.is_valid(), form2.errors
        _sync_energyplus_resources(step, form2)

        # Same resource should still exist with the same PK
        resources = step.step_resources.filter(
            role=WorkflowStepResource.MODEL_TEMPLATE,
        )
        assert resources.count() == 1
        assert resources.first().pk == resource_pk

    def test_template_does_not_affect_weather_file(self):
        """Template operations don't interfere with weather file resources.

        Weather files use ``role=WEATHER_FILE`` and catalog references.
        Template operations only touch ``role=MODEL_TEMPLATE`` rows.
        The weather file resource created by ``_sync_energyplus_resources``
        (from the auto-selected weather file) should survive template
        operations unchanged.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)

        # Create template resource (also syncs weather file)
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            step=step,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        _sync_energyplus_resources(step, form)

        # Both resource types should exist independently
        weather_count = step.step_resources.filter(
            role=WorkflowStepResource.WEATHER_FILE,
        ).count()
        template_count = step.step_resources.filter(
            role=WorkflowStepResource.MODEL_TEMPLATE,
        ).count()
        assert weather_count == 1  # Auto-selected weather file
        assert template_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# save_workflow_step() — file type enforcement
# ══════════════════════════════════════════════════════════════════════════════


class TestFileTypeEnforcement:
    """Tests for JSON file type enforcement on parameterized template steps.

    Parameterized templates require JSON submissions because the submitter
    sends variable values as a JSON payload.  The enforcement check in
    ``save_workflow_step()`` rejects template activation on workflows that
    don't allow JSON file types.
    """

    def test_template_allowed_when_json_in_file_types(self):
        """Template activation succeeds when the workflow allows JSON.

        This is the normal case — the factory default includes JSON in
        ``allowed_file_types``.
        """
        validator = _make_energyplus_validator()
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON],
        )
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors

        step = save_workflow_step(workflow, validator, form)

        assert step.config.get("template_variables") is not None
        assert len(step.config["template_variables"]) == 3  # noqa: PLR2004

    def test_template_rejected_when_json_not_in_file_types(self):
        """Template activation fails when the workflow doesn't allow JSON.

        The author must add JSON to ``allowed_file_types`` before
        activating a parameterized template.
        """
        validator = _make_energyplus_validator()
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.XML],
        )
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors

        with pytest.raises(ValidationError, match="JSON"):
            save_workflow_step(workflow, validator, form)

    def test_no_template_no_enforcement(self):
        """Steps without templates don't trigger file type enforcement.

        Non-template EnergyPlus steps accept any file type — the
        enforcement is only for parameterized templates.
        """
        validator = _make_energyplus_validator()
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.XML],
        )
        form = _make_form(validator=validator)
        assert form.is_valid(), form.errors

        # Should not raise — no template variables in config
        step = save_workflow_step(workflow, validator, form)
        assert "template_variables" not in step.config


# ══════════════════════════════════════════════════════════════════════════════
# EnergyPlusStepConfigForm — template state
# ══════════════════════════════════════════════════════════════════════════════


class TestFormTemplateState:
    """Tests for template-related state on the form instance.

    The form exposes ``has_template`` and ``template_filename`` so that
    the Django template can render the appropriate UI: "upload a template"
    vs "current template: glazing_template.idf [Remove]".
    """

    def test_new_step_has_no_template(self):
        """A form for a new step reports no template."""
        validator = _make_energyplus_validator()
        form = _make_form(validator=validator)

        assert form.has_template is False
        assert form.template_filename == ""

    def test_step_with_template_resource_reports_template(self):
        """A form for an existing step with a template resource shows it.

        The ``has_template`` flag and ``template_filename`` are populated
        from the ``WorkflowStepResource`` query in ``__init__``.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(validator=validator)

        # Create a template resource for the step
        upload = _make_template_upload(filename="glazing.idf")
        form1 = _make_form(
            validator=validator,
            step=step,
            files={"template_file": upload},
        )
        assert form1.is_valid(), form1.errors
        _sync_energyplus_resources(step, form1)

        # Now create a fresh form for the same step
        form2 = _make_form(validator=validator, step=step)

        assert form2.has_template is True
        assert form2.template_filename == "glazing.idf"

    def test_case_sensitive_initial_from_config(self):
        """The ``case_sensitive`` field initial value comes from step config.

        When editing an existing step, the form should show the current
        case sensitivity setting, not the default.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "case_sensitive": False,
            },
        )
        form = _make_form(validator=validator, step=step)

        assert form.fields["case_sensitive"].initial is False


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3: Template Variable Editor — dynamic form fields
# ══════════════════════════════════════════════════════════════════════════════


class TestVariableEditorDynamicFields:
    """Tests for the dynamic per-variable form fields on EnergyPlusStepConfigForm.

    When a step has ``template_variables`` in its config, the form creates
    dynamic fields (``tplvar_0_description``, ``tplvar_0_variable_type``, etc.)
    for each variable.  These fields let the author annotate each variable
    with a type, constraints, default value, and label.

    The fields are excluded from the crispy ``Layout`` and rendered by the
    ``template_variable_editor.html`` partial instead.
    """

    def test_dynamic_fields_created_for_template_variables(self):
        """Dynamic ``tplvar_*`` fields are created when template variables exist.

        Each detected variable gets nine fields: description, default, units,
        variable_type, min_value, min_exclusive, max_value, max_exclusive,
        and choices.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": [
                    {"name": "U_FACTOR", "description": "U-Factor"},
                    {"name": "SHGC", "description": "SHGC"},
                ],
            },
        )
        form = _make_form(validator=validator, step=step)

        # 9 fields per variable x 2 variables = 18 dynamic fields
        tplvar_fields = [f for f in form.fields if f.startswith("tplvar_")]
        assert len(tplvar_fields) == 18  # noqa: PLR2004

        # Verify specific field names
        assert "tplvar_0_description" in form.fields
        assert "tplvar_0_variable_type" in form.fields
        assert "tplvar_1_choices" in form.fields

    def test_no_dynamic_fields_without_template(self):
        """No ``tplvar_*`` fields when the step has no template variables.

        This is the backward-compatible case: pre-template EnergyPlus steps
        have no template-related config keys.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={"idf_checks": [], "run_simulation": False},
        )
        form = _make_form(validator=validator, step=step)

        tplvar_fields = [f for f in form.fields if f.startswith("tplvar_")]
        assert tplvar_fields == []

    def test_dynamic_fields_have_correct_initial_values(self):
        """Dynamic field initial values match the step config.

        When editing an existing step, each variable's fields should be
        pre-populated with the saved annotation data.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": [
                    {
                        "name": "U_FACTOR",
                        "description": "Window U-Factor",
                        "default": "2.0",
                        "units": "W/m2-K",
                        "variable_type": "number",
                        "min_value": 0.1,
                        "min_exclusive": False,
                        "max_value": 7.0,
                        "max_exclusive": True,
                        "choices": [],
                    },
                ],
            },
        )
        form = _make_form(validator=validator, step=step)

        assert form.fields["tplvar_0_description"].initial == "Window U-Factor"
        assert form.fields["tplvar_0_default"].initial == "2.0"
        assert form.fields["tplvar_0_units"].initial == "W/m2-K"
        assert form.fields["tplvar_0_variable_type"].initial == "number"
        assert form.fields["tplvar_0_min_value"].initial == "0.1"
        assert form.fields["tplvar_0_min_exclusive"].initial is False
        assert form.fields["tplvar_0_max_value"].initial == "7.0"
        assert form.fields["tplvar_0_max_exclusive"].initial is True

    def test_new_step_no_dynamic_fields(self):
        """A form for a brand-new step (step=None) has no dynamic fields.

        Template variables only exist after the first template upload and
        save cycle.
        """
        validator = _make_energyplus_validator()
        form = _make_form(validator=validator, step=None)

        tplvar_fields = [f for f in form.fields if f.startswith("tplvar_")]
        assert tplvar_fields == []


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3: template_variable_fields property
# ══════════════════════════════════════════════════════════════════════════════


class TestTemplateVariableFieldsProperty:
    """Tests for the ``template_variable_fields`` form property.

    This property returns a list of dicts, each containing a variable's
    name, index, required/optional badge state, and BoundField objects.
    The template partial iterates over this list to render per-variable
    annotation cards.
    """

    def test_returns_correct_structure(self):
        """Each item has the expected keys for template rendering.

        The ``name`` and ``index`` are strings/ints for display; the field
        keys are BoundField objects that render as HTML form controls.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": [
                    {"name": "U_FACTOR", "description": "U-Factor"},
                ],
            },
        )
        form = _make_form(validator=validator, step=step)

        fields = form.template_variable_fields
        assert len(fields) == 1

        var = fields[0]
        assert var["name"] == "U_FACTOR"
        assert var["index"] == 0
        assert "description" in var
        assert "variable_type" in var
        assert "choices" in var

    def test_is_required_when_no_default(self):
        """Variable is marked required when default is empty.

        The required/optional badge is derived from the default value:
        empty default = required, non-empty default = optional.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": [
                    {"name": "U_FACTOR", "default": ""},
                ],
            },
        )
        form = _make_form(validator=validator, step=step)

        assert form.template_variable_fields[0]["is_required"] is True

    def test_is_optional_when_default_set(self):
        """Variable is marked optional when default is non-empty."""
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": [
                    {"name": "U_FACTOR", "default": "2.0"},
                ],
            },
        )
        form = _make_form(validator=validator, step=step)

        assert form.template_variable_fields[0]["is_required"] is False

    def test_empty_when_no_template_variables(self):
        """Returns empty list when no template variables exist."""
        validator = _make_energyplus_validator()
        form = _make_form(validator=validator, step=None)

        assert form.template_variable_fields == []


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3: Annotation merge in build_energyplus_config()
# ══════════════════════════════════════════════════════════════════════════════


class TestAnnotationMerge:
    """Tests for annotation merge when saving with the variable editor.

    When an existing step has template variables and the author saves the
    form (without uploading a new template or removing the existing one),
    ``build_energyplus_config()`` reads the dynamic ``tplvar_*`` fields
    and merges author-provided annotations into the config.
    """

    def test_annotations_persist_on_save(self):
        """Author annotations from the form are written to config.

        When the author fills in description, default, and type fields,
        those values should appear in the merged ``template_variables``.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": [
                    {"name": "U_FACTOR", "description": "U-Factor", "units": "W/m2-K"},
                ],
            },
        )
        form = _make_form(
            validator=validator,
            step=step,
            data={
                "tplvar_0_description": "Window U-Factor",
                "tplvar_0_default": "2.0",
                "tplvar_0_variable_type": "number",
                "tplvar_0_min_value": "0.1",
                "tplvar_0_max_value": "7.0",
            },
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step)

        var = config["template_variables"][0]
        assert var["name"] == "U_FACTOR"
        assert var["description"] == "Window U-Factor"
        assert var["default"] == "2.0"
        assert var["variable_type"] == "number"
        assert var["min_value"] == 0.1  # noqa: PLR2004
        assert var["max_value"] == 7.0  # noqa: PLR2004

    def test_name_is_immutable(self):
        """Variable names cannot be changed via form data.

        Even if the form data includes a different name, the merge logic
        always uses the name from the existing config.  This prevents
        accidental renaming via DOM manipulation.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": [
                    {"name": "U_FACTOR"},
                ],
            },
        )
        # Note: there's no tplvar_0_name field — the name is not editable.
        # But even if someone adds extra POST data, it won't be in cleaned_data.
        form = _make_form(validator=validator, step=step)
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step)

        assert config["template_variables"][0]["name"] == "U_FACTOR"

    def test_min_max_parsing(self):
        """String min/max values are parsed to floats; empty strings become None.

        The form fields use CharField (not FloatField) so we can distinguish
        "empty" from "0".  The ``_parse_optional_float`` helper converts them.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": [{"name": "U_FACTOR"}],
            },
        )
        form = _make_form(
            validator=validator,
            step=step,
            data={
                "tplvar_0_variable_type": "number",
                "tplvar_0_min_value": "0.5",
                "tplvar_0_max_value": "",
            },
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step)

        var = config["template_variables"][0]
        assert var["min_value"] == 0.5  # noqa: PLR2004
        assert var["max_value"] is None

    def test_choices_parsing(self):
        """Multiline choices are parsed into a list of non-empty strings.

        The textarea value is split by newlines, each line is stripped of
        whitespace, and empty lines are filtered out.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": [{"name": "ROUGHNESS"}],
            },
        )
        form = _make_form(
            validator=validator,
            step=step,
            data={
                "tplvar_0_variable_type": "choice",
                "tplvar_0_choices": "VerySmooth\nSmooth\n\nMediumSmooth\n",
            },
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step)

        var = config["template_variables"][0]
        assert var["choices"] == ["VerySmooth", "Smooth", "MediumSmooth"]

    def test_type_change_preserves_all_fields(self):
        """Changing variable_type stores all annotation fields.

        Even when the author changes from 'number' to 'text', min/max
        values are still stored in the config.  The UI hides them, but
        the data is preserved in case the author switches back.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": [
                    {
                        "name": "U_FACTOR",
                        "variable_type": "number",
                        "min_value": 0.1,
                        "max_value": 7.0,
                    },
                ],
            },
        )
        form = _make_form(
            validator=validator,
            step=step,
            data={
                "tplvar_0_variable_type": "text",
                # min/max still in POST data from hidden fields
                "tplvar_0_min_value": "0.1",
                "tplvar_0_max_value": "7.0",
            },
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step)

        var = config["template_variables"][0]
        assert var["variable_type"] == "text"
        assert var["min_value"] == 0.1  # noqa: PLR2004
        assert var["max_value"] == 7.0  # noqa: PLR2004

    def test_multiple_variables_merge_independently(self):
        """Each variable's annotations are merged independently.

        Annotations for variable 0 don't bleed into variable 1.
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "idf_checks": [],
                "run_simulation": False,
                "template_variables": [
                    {"name": "U_FACTOR"},
                    {"name": "SHGC"},
                ],
            },
        )
        form = _make_form(
            validator=validator,
            step=step,
            data={
                "tplvar_0_description": "Window U-Factor",
                "tplvar_0_variable_type": "number",
                "tplvar_1_description": "Solar Heat Gain Coefficient",
                "tplvar_1_variable_type": "text",
            },
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step)

        assert config["template_variables"][0]["description"] == "Window U-Factor"
        assert config["template_variables"][0]["variable_type"] == "number"
        assert (
            config["template_variables"][1]["description"]
            == "Solar Heat Gain Coefficient"
        )
        assert config["template_variables"][1]["variable_type"] == "text"


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3: Display signals field
# ══════════════════════════════════════════════════════════════════════════════


class TestDisplaySignals:
    """Tests for the ``display_signals`` MultipleChoiceField.

    Output signal selection lets the author choose which EnergyPlus output
    signals to display in submission results.  Choices are populated from
    the validator's output catalog entries.
    """

    def test_display_signals_field_exists(self):
        """The ``display_signals`` field is always present on the form."""
        validator = _make_energyplus_validator()
        form = _make_form(validator=validator)

        assert "display_signals" in form.fields

    def test_display_signals_choices_empty_without_catalog_entries(self):
        """When the validator has no output catalog entries, choices are empty.

        This is the typical case for a freshly created test validator.
        """
        validator = _make_energyplus_validator()
        form = _make_form(validator=validator)

        assert form.fields["display_signals"].choices == []


# ===========================================================================
# Phase 4: Launcher integration tests
# ===========================================================================
# These tests exercise `launch_energyplus_validation()` with real Django
# models (factories create actual DB rows) but mock the external I/O
# layer (GCS uploads, Cloud Run job triggering, callback URL building).
#
# The launcher is the orchestration point where template mode detection,
# parameter validation, IDF substitution, and envelope building all
# converge.  Integration tests verify this wiring — the individual
# functions (`merge_and_validate_template_parameters`, `substitute_
# template_parameters`) have thorough unit tests in `test_idf_template.py`.
# ===========================================================================

# Template IDF used in launcher integration tests.  Identical to
# VALID_TEMPLATE_IDF above but kept separate so launcher tests are
# self-contained if the form tests evolve.
_LAUNCHER_TEMPLATE_IDF = """\
Version,
    24.2;  !- Version Identifier

WindowMaterial:SimpleGlazingSystem,
    Glazing System,          !- Name
    $U_FACTOR,               !- U-Factor {W/m2-K}
    $SHGC;                   !- Solar Heat Gain Coefficient
"""

# Common mock targets — all live in the launcher module's namespace.
_PATCH_PREFIX = "validibot.validations.services.cloud_run.launcher"


def _launcher_mocks():
    """Return a dict of patch objects for the four external I/O functions.

    Usage::

        with _launcher_mocks() as mocks:
            mocks["run_validator_job"].return_value = "exec-abc"
            result = launch_energyplus_validation(...)
    """
    return {
        "upload_file": patch(f"{_PATCH_PREFIX}.upload_file"),
        "upload_envelope": patch(f"{_PATCH_PREFIX}.upload_envelope"),
        "run_validator_job": patch(
            f"{_PATCH_PREFIX}.run_validator_job",
            return_value="executions/test-exec-001",
        ),
        "build_callback_url": patch(
            f"{_PATCH_PREFIX}.build_validation_callback_url",
            return_value="https://worker.test/api/v1/validation-callbacks/",
        ),
    }


class _LauncherMocks:
    """Context manager that patches all external I/O for launcher tests.

    Provides attribute access to each mock so tests can inspect call args.
    Also patches Django settings required by the launcher (GCS bucket,
    Cloud Run job name, GCP project/region).
    """

    def __enter__(self):
        self._patchers = []
        self._settings_patcher = patch.multiple(
            "django.conf.settings",
            GCS_VALIDATION_BUCKET="test-bucket",
            GCS_ENERGYPLUS_JOB_NAME="energyplus-job",
            GCP_PROJECT_ID="test-project",
            GCP_REGION="us-central1",
        )
        self._settings_patcher.start()
        self._patchers.append(self._settings_patcher)

        targets = _launcher_mocks()
        for name, patcher in targets.items():
            mock_obj = patcher.start()
            setattr(self, name, mock_obj)
            self._patchers.append(patcher)

        # Default return value for job execution name
        self.run_validator_job.return_value = "executions/test-exec-001"
        self.build_callback_url.return_value = (
            "https://worker.test/api/v1/validation-callbacks/"
        )
        return self

    def __exit__(self, *exc):
        for patcher in reversed(self._patchers):
            patcher.stop()


def _make_launcher_fixtures(
    *,
    template_content: str | None = None,
    submission_content: str = "{}",
    step_config: dict | None = None,
):
    """Create the full model graph needed by ``launch_energyplus_validation()``.

    Creates: Validator → WorkflowStep → WorkflowStepResource(WEATHER_FILE)
    → Submission → ValidationRun → ValidationStepRun(PENDING).

    If ``template_content`` is provided, also creates a
    ``WorkflowStepResource(MODEL_TEMPLATE)`` with that IDF content as the
    step-owned file.

    Returns a dict with keys: ``run``, ``validator``, ``submission``,
    ``step``, ``step_run``.
    """
    from validibot.submissions.tests.factories import SubmissionFactory

    # 1. Validator (EnergyPlus type)
    validator = ValidatorFactory(
        validation_type=ValidationType.ENERGYPLUS,
    )

    # 2. Workflow and step — step must belong to the run's workflow,
    #    so we create the workflow first, then wire everything through it.
    workflow = WorkflowFactory(org=validator.org or WorkflowFactory().org)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        config=step_config or {},
    )

    # 3. Weather file resource (required by the envelope builder)
    weather_vrf = ValidatorResourceFileFactory(validator=validator)
    WorkflowStepResourceFactory(
        step=step,
        role=WorkflowStepResource.WEATHER_FILE,
        validator_resource_file=weather_vrf,
    )

    # 4. Optionally add a template resource (step-owned file)
    if template_content is not None:
        WorkflowStepResourceFactory(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            validator_resource_file=None,
            step_resource_file=SimpleUploadedFile(
                "template.idf",
                template_content.encode("utf-8"),
                content_type="text/plain",
            ),
            filename="template.idf",
            resource_type="energyplus_model_template",
        )

    # 5. Submission with specified content
    submission = SubmissionFactory(
        workflow=workflow,
        org=workflow.org,
        content=submission_content,
    )

    # 6. ValidationRun + StepRun (PENDING)
    step_run = ValidationStepRunFactory(
        validation_run__workflow=workflow,
        validation_run__org=workflow.org,
        validation_run__submission=submission,
        workflow_step=step,
    )
    run = step_run.validation_run

    return {
        "run": run,
        "validator": validator,
        "submission": submission,
        "step": step,
        "step_run": step_run,
    }


class TestLauncherDirectMode:
    """Tests for the direct (non-template) code path in the launcher.

    Direct mode is the original behavior: the submission is a complete
    IDF or epJSON file that gets uploaded directly to GCS.  These tests
    verify that the template mode changes didn't break existing behavior.
    """

    def test_direct_mode_uploads_submission_and_returns_pending(self):
        """When no MODEL_TEMPLATE resource exists, the submission is uploaded
        as-is to GCS and the launcher returns a pending result.

        This is the regression guard for the template mode refactor — the
        original direct path must remain unchanged.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        fixtures = _make_launcher_fixtures(
            submission_content='{"version": "24.2"}',
        )

        with _LauncherMocks() as mocks:
            result = launch_energyplus_validation(
                run=fixtures["run"],
                validator=fixtures["validator"],
                submission=fixtures["submission"],
                ruleset=None,
                step=fixtures["step"],
            )

        # Should return pending (passed=None, no issues)
        assert result.passed is None
        assert result.issues == []
        assert result.stats["job_name"] == "energyplus-job"
        assert result.stats["execution_name"] == "executions/test-exec-001"

        # Template metadata should NOT be present
        assert "template_parameters_used" not in result.stats
        assert "template_warnings" not in result.stats

        # upload_file should have been called with the raw submission content
        upload_call = mocks.upload_file.call_args
        assert b'{"version": "24.2"}' in upload_call.kwargs.get(
            "content", upload_call[1].get("content", b"")
        )

    def test_direct_mode_no_template_metadata_in_stats(self):
        """Direct mode stats contain job info but no template-related keys.

        This confirms the ``**template_metadata`` spread in the stats dict
        correctly produces an empty merge when there is no template.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        fixtures = _make_launcher_fixtures()

        with _LauncherMocks():
            result = launch_energyplus_validation(
                run=fixtures["run"],
                validator=fixtures["validator"],
                submission=fixtures["submission"],
                ruleset=None,
                step=fixtures["step"],
            )

        # Core stats present
        assert "job_status" in result.stats
        assert "execution_bundle_uri" in result.stats

        # No template keys
        for key in (
            "template_parameters_used",
            "template_warnings",
            "template_original_uri",
        ):
            assert key not in result.stats


class TestLauncherTemplateMode:
    """Tests for the template mode code path in the launcher.

    Template mode activates when the step has a ``WorkflowStepResource``
    with ``role=MODEL_TEMPLATE``.  The submission is JSON parameters that
    get merged, validated, and substituted into the template IDF.  The
    container receives a resolved IDF with no ``$VARIABLE`` placeholders.
    """

    def test_template_mode_detected_and_resolved_idf_uploaded(self):
        """When a MODEL_TEMPLATE resource exists, the launcher reads the
        template, substitutes parameters, and uploads the resolved IDF.

        This is the core happy-path test for the template pipeline.  We
        verify that ``upload_file`` is called with the resolved IDF content
        (placeholders replaced) rather than the raw JSON submission.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        submitter_params = json.dumps({"U_FACTOR": "2.5", "SHGC": "0.4"})

        fixtures = _make_launcher_fixtures(
            template_content=_LAUNCHER_TEMPLATE_IDF,
            submission_content=submitter_params,
            step_config={
                "template_variables": [
                    {"name": "U_FACTOR", "variable_type": "number"},
                    {"name": "SHGC", "variable_type": "number"},
                ],
                "case_sensitive": True,
            },
        )

        with _LauncherMocks() as mocks:
            result = launch_energyplus_validation(
                run=fixtures["run"],
                validator=fixtures["validator"],
                submission=fixtures["submission"],
                ruleset=None,
                step=fixtures["step"],
            )

        # Should return pending
        assert result.passed is None
        assert result.issues == []

        # upload_file should be called for both the resolved IDF and
        # the original template (at minimum).
        expected_min_uploads = 2  # model.idf + template_original.idf
        assert mocks.upload_file.call_count >= expected_min_uploads

        # Find the resolved IDF upload (the one to model.idf)
        resolved_call = None
        template_call = None
        for call in mocks.upload_file.call_args_list:
            uri = call.kwargs.get("uri", call[1].get("uri", ""))
            if "model.idf" in uri and "template_original" not in uri:
                resolved_call = call
            elif "template_original" in uri:
                template_call = call

        # Resolved IDF should contain substituted values, not placeholders
        assert resolved_call is not None, "No upload_file call for model.idf"
        resolved_content = resolved_call.kwargs.get(
            "content", resolved_call[1].get("content", b"")
        ).decode("utf-8")
        assert "2.5" in resolved_content
        assert "0.4" in resolved_content
        assert "$U_FACTOR" not in resolved_content
        assert "$SHGC" not in resolved_content

        # Original template should be uploaded for audit trail
        assert template_call is not None, "No upload_file call for template_original"

    def test_template_metadata_in_stats(self):
        """Template mode stats include parameter values, warnings, and the
        original template URI so audit and debugging are possible.

        These metadata keys are merged into the step run's output and
        persisted alongside the usual job tracking info.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        submitter_params = json.dumps({"U_FACTOR": "2.5", "SHGC": "0.4"})

        fixtures = _make_launcher_fixtures(
            template_content=_LAUNCHER_TEMPLATE_IDF,
            submission_content=submitter_params,
            step_config={
                "template_variables": [
                    {"name": "U_FACTOR", "variable_type": "number"},
                    {"name": "SHGC", "variable_type": "number"},
                ],
                "case_sensitive": True,
            },
        )

        with _LauncherMocks():
            result = launch_energyplus_validation(
                run=fixtures["run"],
                validator=fixtures["validator"],
                submission=fixtures["submission"],
                ruleset=None,
                step=fixtures["step"],
            )

        # Template metadata should be present in stats
        assert "template_parameters_used" in result.stats
        assert result.stats["template_parameters_used"] == {
            "U_FACTOR": "2.5",
            "SHGC": "0.4",
        }
        assert "template_warnings" in result.stats
        assert isinstance(result.stats["template_warnings"], list)
        assert "template_original_uri" in result.stats
        assert "template_original" in result.stats["template_original_uri"]

    def test_merge_validation_error_returns_failed(self):
        """When submitter parameters fail validation (e.g., missing required
        variable), the launcher returns ``passed=False`` with user-friendly
        error issues rather than an unhandled exception.

        The ``ValidationError`` raised by ``merge_and_validate_template_
        parameters()`` is caught by the launcher's ``except ValidationError``
        handler and converted to ``ValidationIssue`` objects with
        ``path="template_parameters"``.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        # Submit empty params but template variables are required (no defaults)
        submitter_params = json.dumps({})

        fixtures = _make_launcher_fixtures(
            template_content=_LAUNCHER_TEMPLATE_IDF,
            submission_content=submitter_params,
            step_config={
                "template_variables": [
                    {"name": "U_FACTOR", "variable_type": "number"},
                    {"name": "SHGC", "variable_type": "number"},
                ],
                "case_sensitive": True,
            },
        )

        with _LauncherMocks() as mocks:
            result = launch_energyplus_validation(
                run=fixtures["run"],
                validator=fixtures["validator"],
                submission=fixtures["submission"],
                ruleset=None,
                step=fixtures["step"],
            )

        # Should fail (not pending, not passed)
        assert result.passed is False
        assert len(result.issues) >= 1

        # Issues should reference template_parameters path
        assert all(issue.path == "template_parameters" for issue in result.issues)

        # Error messages should mention the missing variable names
        messages = [issue.message for issue in result.issues]
        combined = " ".join(messages)
        assert "U_FACTOR" in combined
        assert "SHGC" in combined

        # No GCS upload or job trigger should happen on validation failure
        mocks.upload_file.assert_not_called()
        mocks.run_validator_job.assert_not_called()

    def test_template_defaults_fill_missing_params(self):
        """When a template variable has a default value and the submitter
        omits it, the default is used in the resolved IDF.

        This verifies the merge logic integration: author-defined defaults
        from the step config fill gaps in the submitter's JSON payload.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        # Only provide U_FACTOR; SHGC has a default
        submitter_params = json.dumps({"U_FACTOR": "3.0"})

        fixtures = _make_launcher_fixtures(
            template_content=_LAUNCHER_TEMPLATE_IDF,
            submission_content=submitter_params,
            step_config={
                "template_variables": [
                    {"name": "U_FACTOR", "variable_type": "number"},
                    {
                        "name": "SHGC",
                        "variable_type": "number",
                        "default": "0.25",
                    },
                ],
                "case_sensitive": True,
            },
        )

        with _LauncherMocks() as mocks:
            result = launch_energyplus_validation(
                run=fixtures["run"],
                validator=fixtures["validator"],
                submission=fixtures["submission"],
                ruleset=None,
                step=fixtures["step"],
            )

        # Should succeed (pending)
        assert result.passed is None
        assert result.issues == []

        # Template metadata should show the default was used
        assert result.stats["template_parameters_used"]["SHGC"] == "0.25"
        assert result.stats["template_parameters_used"]["U_FACTOR"] == "3.0"

        # Resolved IDF should contain the default value
        resolved_call = None
        for call in mocks.upload_file.call_args_list:
            uri = call.kwargs.get("uri", call[1].get("uri", ""))
            if "model.idf" in uri and "template_original" not in uri:
                resolved_call = call
                break

        assert resolved_call is not None
        resolved_content = resolved_call.kwargs.get(
            "content", resolved_call[1].get("content", b"")
        ).decode("utf-8")
        assert "3.0" in resolved_content
        assert "0.25" in resolved_content
