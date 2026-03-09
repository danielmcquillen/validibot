"""
Integration tests for the EnergyPlus parameterized template workflow.

These tests verify the end-to-end pipeline from form submission through
config building and resource syncing:

1. ``build_energyplus_config()`` — Validates uploaded IDF files, scans for
   ``$VARIABLE_NAME`` placeholders, populates ``template_variables`` in the
   config dict, handles template removal, and preserves existing config
   as-is when no upload occurs.

2. ``_sync_energyplus_resources()`` — Creates/deletes ``WorkflowStepResource``
   rows with ``role=MODEL_TEMPLATE`` for step-owned template files.

3. ``save_workflow_step()`` — File type enforcement ensures workflows with
   parameterized templates accept JSON submissions.

4. Template Variable Annotation — ``TemplateVariableAnnotationForm`` (a
   standalone form) creates dynamic ``tplvar_*`` fields for per-variable
   annotation.  ``merge_template_variable_annotations()`` (an extracted
   helper in ``views_helpers``) merges author annotations into the stored
   config.  These are tested independently of ``build_energyplus_config()``.

5. ``launch_energyplus_validation()`` (Phase 4) — Launcher integration
   testing with real Django models and mocked GCS/Cloud Run I/O to verify
   the upload and job-trigger pipeline.  Template preprocessing (parameter
   merging, validation, substitution) happens earlier in the pipeline in
   ``EnergyPlusValidator.preprocess_submission()`` — those tests live in
   ``test_energyplus_preprocessing.py``.

Unlike the pure-Python scanner tests in ``test_idf_template.py``, these
tests require a Django database because they exercise form objects, model
instances, and ORM queries.

Phases: 2-4 of the EnergyPlus Parameterized Templates ADR.
"""

from __future__ import annotations

from http import HTTPStatus
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
from validibot.workflows.forms import TemplateVariableAnnotationForm
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.tests.factories import WorkflowStepResourceFactory
from validibot.workflows.views_helpers import _sync_energyplus_resources
from validibot.workflows.views_helpers import build_energyplus_config
from validibot.workflows.views_helpers import merge_template_variable_annotations
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

    # Infer a sensible validation_mode default: template mode when the
    # test uploads a template file or the step already has template_variables.
    has_template_context = bool(files and files.get("template_file"))
    if step and (step.config or {}).get("template_variables"):
        has_template_context = True
    default_mode = (
        EnergyPlusStepConfigForm.VALIDATION_MODE_TEMPLATE
        if has_template_context
        else EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT
    )

    defaults = {
        "name": "Test Step",
        "weather_file": weather_file_id,
        "validation_mode": default_mode,
        "idf_checks": [],
        "run_simulation": False,
        "case_sensitive": True,
        "remove_template": False,
    }

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
# build_energyplus_config() — validation_mode routing
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildConfigValidationMode:
    """Tests for the ``validation_mode`` field that routes config building.

    The form's ``validation_mode`` radio selector determines which config keys
    are populated by ``build_energyplus_config()``.  Direct mode stores IDF
    check/simulation settings and clears template metadata.  Template mode
    stores template variables, case sensitivity, and display signals.
    """

    def test_direct_mode_stores_idf_settings(self):
        """Direct mode populates ``idf_checks`` and ``run_simulation``.

        These are the settings relevant when submitters upload a complete IDF
        file for validation.
        """
        validator = _make_energyplus_validator()
        form = _make_form(
            validator=validator,
            data={
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT,
                "idf_checks": ["duplicate-names"],
                "run_simulation": True,
            },
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form)

        assert config["validation_mode"] == "direct"
        assert config["idf_checks"] == ["duplicate-names"]
        assert config["run_simulation"] is True

    def test_direct_mode_clears_template_metadata(self):
        """Direct mode clears template variables and display signals.

        Even if template data existed before, switching to direct mode should
        produce empty template metadata so the step no longer expects JSON
        parameter submissions.
        """
        validator = _make_energyplus_validator()
        form = _make_form(
            validator=validator,
            data={
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT,
            },
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form)

        assert config["template_variables"] == []
        assert config["display_signals"] == []
        assert config["case_sensitive"] is True

    def test_template_mode_stores_template_settings(self):
        """Template mode populates template-specific config keys.

        When a template file is uploaded, the config should include
        ``template_variables`` from the IDF scan, plus the author's
        ``case_sensitive`` and ``display_signals`` preferences.
        """
        validator = _make_energyplus_validator()
        upload = _make_template_upload()
        form = _make_form(
            validator=validator,
            data={
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_TEMPLATE,
            },
            files={"template_file": upload},
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form)

        assert config["validation_mode"] == "template"
        expected_var_count = 3
        assert len(config["template_variables"]) == expected_var_count
        assert config["idf_checks"] == []
        assert config["run_simulation"] is True

    def test_direct_mode_signals_template_removal(self):
        """Direct mode sets ``remove_template`` in cleaned_data.

        This tells ``_sync_energyplus_resources()`` to delete any existing
        template file resource from a previous template-mode configuration.
        """
        validator = _make_energyplus_validator()
        form = _make_form(
            validator=validator,
            data={
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT,
            },
        )
        assert form.is_valid(), form.errors
        build_energyplus_config(form)

        assert form.cleaned_data["remove_template"] is True

    def test_validation_mode_persisted_in_config(self):
        """The chosen validation mode is stored in the config dict.

        This allows the step details card and other views to display the
        correct mode label without inspecting resource files.
        """
        validator = _make_energyplus_validator()
        for mode in ("direct", "template"):
            data = {"validation_mode": mode}
            if mode == "template":
                files = {"template_file": _make_template_upload()}
            else:
                files = None
            form = _make_form(validator=validator, data=data, files=files)
            assert form.is_valid(), form.errors
            config = build_energyplus_config(form)
            assert config["validation_mode"] == mode


# ══════════════════════════════════════════════════════════════════════════════
# build_energyplus_config() — template removal
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildConfigWithTemplateRemoval:
    """Tests for ``build_energyplus_config`` when the template is removed.

    Removal means the author is switching from template mode back to direct
    IDF submission.  All template metadata should be cleared from config.
    """

    def test_remove_clears_template_variables(self):
        """Switching to direct mode clears the ``template_variables`` list.

        Even if the step had template variables before, the config should
        come back with an empty list because the author selected direct mode.
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
            data={
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT,
                "remove_template": True,
            },
        )
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step)

        assert config["template_variables"] == []
        assert config["case_sensitive"] is True
        assert config["display_signals"] == []

    def test_remove_preserves_non_template_config(self):
        """Simulation settings are preserved when switching to direct mode.

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
                "validation_mode": EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT,
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

        When no template upload or removal occurs, ``build_energyplus_config()``
        preserves existing ``template_variables`` from the step's config as-is.
        Annotation editing now happens in the dedicated template variables card
        (via ``merge_template_variable_annotations``), not in the step config form.
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
        """A new step (no existing step) in direct mode has empty template metadata.

        When ``step=None`` and no template is uploaded, the config should
        contain simulation settings with empty template metadata (direct mode
        always clears template data).
        """
        validator = _make_energyplus_validator()
        form = _make_form(validator=validator)
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step=None)

        assert config["template_variables"] == []
        assert config["display_signals"] == []

    def test_step_without_template_has_empty_template_keys(self):
        """An existing step in direct mode has empty template metadata.

        Pre-template steps have ``config={"idf_checks": [], "run_simulation": False}``
        with no template keys.  Re-saving in direct mode produces empty
        template lists (consistent cleanup).
        """
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={"idf_checks": [], "run_simulation": False},
        )
        form = _make_form(validator=validator, step=step)
        assert form.is_valid(), form.errors
        config = build_energyplus_config(form, step)

        assert config["template_variables"] == []
        assert config["display_signals"] == []


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

        # Should not raise — direct mode has empty template variables
        step = save_workflow_step(workflow, validator, form)
        assert step.config["template_variables"] == []


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
# TemplateVariableAnnotationForm — dynamic form fields
# ══════════════════════════════════════════════════════════════════════════════


def _make_annotation_form(step):
    """Create a ``TemplateVariableAnnotationForm`` bound to the given step.

    This is the standalone form that renders the per-variable annotation
    card on the step detail page.  It reads ``template_variables`` from
    ``step.config`` and creates dynamic ``tplvar_*`` fields.
    """
    return TemplateVariableAnnotationForm(step=step)


class TestVariableEditorDynamicFields:
    """Tests for the dynamic per-variable fields on TemplateVariableAnnotationForm.

    When a step has ``template_variables`` in its config, the standalone
    annotation form creates dynamic fields (``tplvar_0_description``,
    ``tplvar_0_variable_type``, etc.) for each variable.  These fields
    let the author annotate each variable with a type, constraints,
    default value, and label.
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
                "template_variables": [
                    {"name": "U_FACTOR", "description": "U-Factor"},
                    {"name": "SHGC", "description": "SHGC"},
                ],
            },
        )
        form = _make_annotation_form(step)

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
        form = _make_annotation_form(step)

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
        form = _make_annotation_form(step)

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
        form = TemplateVariableAnnotationForm(step=None)

        tplvar_fields = [f for f in form.fields if f.startswith("tplvar_")]
        assert tplvar_fields == []


# ══════════════════════════════════════════════════════════════════════════════
# TemplateVariableAnnotationForm.template_variable_fields property
# ══════════════════════════════════════════════════════════════════════════════


class TestTemplateVariableFieldsProperty:
    """Tests for ``TemplateVariableAnnotationForm.template_variable_fields``.

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
                "template_variables": [
                    {"name": "U_FACTOR", "description": "U-Factor"},
                ],
            },
        )
        form = _make_annotation_form(step)

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
                "template_variables": [
                    {"name": "U_FACTOR", "default": ""},
                ],
            },
        )
        form = _make_annotation_form(step)

        assert form.template_variable_fields[0]["is_required"] is True

    def test_is_optional_when_default_set(self):
        """Variable is marked optional when default is non-empty."""
        validator = _make_energyplus_validator()
        step = WorkflowStepFactory(
            validator=validator,
            config={
                "template_variables": [
                    {"name": "U_FACTOR", "default": "2.0"},
                ],
            },
        )
        form = _make_annotation_form(step)

        assert form.template_variable_fields[0]["is_required"] is False

    def test_empty_when_no_template_variables(self):
        """Returns empty list when no template variables exist."""
        form = TemplateVariableAnnotationForm(step=None)

        assert form.template_variable_fields == []


# ══════════════════════════════════════════════════════════════════════════════
# merge_template_variable_annotations() — annotation merge
# ══════════════════════════════════════════════════════════════════════════════


class TestAnnotationMerge:
    """Tests for ``merge_template_variable_annotations()``.

    This extracted helper takes existing template variable dicts and
    form data with ``tplvar_*`` keys, then returns a new list of
    variable dicts with author annotations merged in.  It is called by
    ``WorkflowStepTemplateVariablesView`` when saving annotations from
    the step detail page's template variables card.
    """

    def test_annotations_persist_on_save(self):
        """Author annotations from form data are merged into the variable dicts.

        When the author fills in description, default, and type fields,
        those values should appear in the merged output.
        """
        existing_vars = [
            {"name": "U_FACTOR", "description": "U-Factor", "units": "W/m2-K"},
        ]
        form_data = {
            "tplvar_0_description": "Window U-Factor",
            "tplvar_0_default": "2.0",
            "tplvar_0_variable_type": "number",
            "tplvar_0_units": "W/m2-K",
            "tplvar_0_min_value": "0.1",
            "tplvar_0_min_exclusive": False,
            "tplvar_0_max_value": "7.0",
            "tplvar_0_max_exclusive": False,
            "tplvar_0_choices": "",
        }
        result = merge_template_variable_annotations(existing_vars, form_data)

        var = result[0]
        assert var["name"] == "U_FACTOR"
        assert var["description"] == "Window U-Factor"
        assert var["default"] == "2.0"
        assert var["variable_type"] == "number"
        assert var["min_value"] == 0.1  # noqa: PLR2004
        assert var["max_value"] == 7.0  # noqa: PLR2004

    def test_name_is_immutable(self):
        """Variable names cannot be changed via form data.

        The merge logic always uses the name from the existing variable
        dict.  Even if someone tampers with form data to include a name
        field, it is ignored.
        """
        existing_vars = [{"name": "U_FACTOR"}]
        form_data = {
            "tplvar_0_description": "",
            "tplvar_0_default": "",
            "tplvar_0_units": "",
            "tplvar_0_variable_type": "text",
            "tplvar_0_min_value": "",
            "tplvar_0_min_exclusive": False,
            "tplvar_0_max_value": "",
            "tplvar_0_max_exclusive": False,
            "tplvar_0_choices": "",
        }
        result = merge_template_variable_annotations(existing_vars, form_data)

        assert result[0]["name"] == "U_FACTOR"

    def test_min_max_parsing(self):
        """String min/max values are parsed to floats; empty strings become None.

        The form fields use CharField (not FloatField) so we can distinguish
        "empty" from "0".  The ``_parse_optional_float`` helper converts them.
        """
        existing_vars = [{"name": "U_FACTOR"}]
        form_data = {
            "tplvar_0_description": "",
            "tplvar_0_default": "",
            "tplvar_0_units": "",
            "tplvar_0_variable_type": "number",
            "tplvar_0_min_value": "0.5",
            "tplvar_0_min_exclusive": False,
            "tplvar_0_max_value": "",
            "tplvar_0_max_exclusive": False,
            "tplvar_0_choices": "",
        }
        result = merge_template_variable_annotations(existing_vars, form_data)

        var = result[0]
        assert var["min_value"] == 0.5  # noqa: PLR2004
        assert var["max_value"] is None

    def test_choices_parsing(self):
        """Multiline choices are parsed into a list of non-empty strings.

        The textarea value is split by newlines, each line is stripped of
        whitespace, and empty lines are filtered out.
        """
        existing_vars = [{"name": "ROUGHNESS"}]
        form_data = {
            "tplvar_0_description": "",
            "tplvar_0_default": "",
            "tplvar_0_units": "",
            "tplvar_0_variable_type": "choice",
            "tplvar_0_min_value": "",
            "tplvar_0_min_exclusive": False,
            "tplvar_0_max_value": "",
            "tplvar_0_max_exclusive": False,
            "tplvar_0_choices": "VerySmooth\nSmooth\n\nMediumSmooth\n",
        }
        result = merge_template_variable_annotations(existing_vars, form_data)

        var = result[0]
        assert var["choices"] == ["VerySmooth", "Smooth", "MediumSmooth"]

    def test_type_change_preserves_all_fields(self):
        """Changing variable_type stores all annotation fields.

        Even when the author changes from 'number' to 'text', min/max
        values are still stored.  The UI hides them, but the data is
        preserved in case the author switches back.
        """
        existing_vars = [
            {
                "name": "U_FACTOR",
                "variable_type": "number",
                "min_value": 0.1,
                "max_value": 7.0,
            },
        ]
        form_data = {
            "tplvar_0_description": "",
            "tplvar_0_default": "",
            "tplvar_0_units": "",
            "tplvar_0_variable_type": "text",
            "tplvar_0_min_value": "0.1",
            "tplvar_0_min_exclusive": False,
            "tplvar_0_max_value": "7.0",
            "tplvar_0_max_exclusive": False,
            "tplvar_0_choices": "",
        }
        result = merge_template_variable_annotations(existing_vars, form_data)

        var = result[0]
        assert var["variable_type"] == "text"
        assert var["min_value"] == 0.1  # noqa: PLR2004
        assert var["max_value"] == 7.0  # noqa: PLR2004

    def test_multiple_variables_merge_independently(self):
        """Each variable's annotations are merged independently.

        Annotations for variable 0 don't bleed into variable 1.
        """
        existing_vars = [
            {"name": "U_FACTOR"},
            {"name": "SHGC"},
        ]
        form_data = {
            "tplvar_0_description": "Window U-Factor",
            "tplvar_0_default": "",
            "tplvar_0_units": "",
            "tplvar_0_variable_type": "number",
            "tplvar_0_min_value": "",
            "tplvar_0_min_exclusive": False,
            "tplvar_0_max_value": "",
            "tplvar_0_max_exclusive": False,
            "tplvar_0_choices": "",
            "tplvar_1_description": "Solar Heat Gain Coefficient",
            "tplvar_1_default": "",
            "tplvar_1_units": "",
            "tplvar_1_variable_type": "text",
            "tplvar_1_min_value": "",
            "tplvar_1_min_exclusive": False,
            "tplvar_1_max_value": "",
            "tplvar_1_max_exclusive": False,
            "tplvar_1_choices": "",
        }
        result = merge_template_variable_annotations(existing_vars, form_data)

        assert result[0]["description"] == "Window U-Factor"
        assert result[0]["variable_type"] == "number"
        assert result[1]["description"] == "Solar Heat Gain Coefficient"
        assert result[1]["variable_type"] == "text"


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3: Display signals field
# ══════════════════════════════════════════════════════════════════════════════


class TestDisplaySignals:
    """Tests for the ``DisplaySignalsForm`` (modal-based signal selection).

    Output signal selection lets the author choose which output signals
    to display in submission results.  This is now a cross-validator
    feature using a standalone form in a modal, not inline in the step
    config form.
    """

    def test_display_signals_not_on_step_config_form(self):
        """The ``display_signals`` field was moved off the step config form.

        It's now edited via a standalone modal (``DisplaySignalsForm``),
        not inline in the EnergyPlus step config form.
        """
        validator = _make_energyplus_validator()
        form = _make_form(validator=validator)

        assert "display_signals" not in form.fields

    def test_display_signals_form_choices_empty_without_catalog_entries(self):
        """When the validator has no output catalog entries, choices are empty.

        This is the typical case for a freshly created test validator.
        """
        from validibot.workflows.forms import DisplaySignalsForm

        validator = _make_energyplus_validator()
        form = DisplaySignalsForm(validator=validator)

        assert form.fields["display_signals"].choices == []


# ══════════════════════════════════════════════════════════════════════════════
# Unified signals — build_unified_signals() helper
# ══════════════════════════════════════════════════════════════════════════════
# ADR-2026-03-10 introduced a unified "Inputs and Outputs" card that merges
# catalog entries (from the validator config) with template variables (from
# the step config) into a single view.  ``build_unified_signals()`` is the
# view-layer helper that builds this merged representation.
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildUnifiedSignals:
    """Tests for ``build_unified_signals()`` — the view helper that merges
    catalog entries and template variables into unified signal lists.

    The helper produces four keys: ``input_signals``, ``output_signals``,
    ``has_inputs``, ``has_outputs``.  Input signals come from two sources:
    catalog INPUT entries (source="catalog") and template variables
    (source="template").  Output signals come only from catalog OUTPUT
    entries, each annotated with ``show_to_user`` based on the step's
    ``display_signals`` config.
    """

    def test_template_variables_only(self):
        """Steps with template variables but no catalog entries still show inputs.

        This is the typical case for an EnergyPlus template-mode step
        before catalog entries have been synced.
        """
        from validibot.workflows.views_helpers import build_unified_signals

        step = WorkflowStepFactory(
            config={
                "template_variables": [
                    {"name": "U_FACTOR", "description": "U-Factor", "default": ""},
                    {"name": "SHGC", "description": "", "default": "0.4"},
                ],
            },
        )

        result = build_unified_signals(catalog_display=None, step=step)

        assert result["has_inputs"] is True
        assert result["has_outputs"] is False
        input_signals = result["input_signals"]
        assert len(input_signals) == len(step.config["template_variables"])

        # First variable: no default → required
        sig0 = result["input_signals"][0]
        assert sig0["slug"] == "$U_FACTOR"
        assert sig0["label"] == "U-Factor"
        assert sig0["source"] == "template"
        assert sig0["required"] is True
        assert sig0["variable_index"] == 0

        # Second variable: has default → not required
        sig1 = result["input_signals"][1]
        assert sig1["slug"] == "$SHGC"
        assert sig1["label"] == "SHGC"  # Falls back to name when no description
        assert sig1["source"] == "template"
        assert sig1["required"] is False
        assert sig1["variable_index"] == 1

    def test_catalog_entries_only(self):
        """Steps with catalog entries but no template variables.

        This is the typical case for validators like FMU or THERM that
        define their signals entirely through the catalog.
        """
        from validibot.validations.models import CatalogDisplay
        from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
        from validibot.workflows.views_helpers import build_unified_signals

        validator = _make_energyplus_validator()
        input_entry = ValidatorCatalogEntryFactory(
            validator=validator,
            run_stage="input",
            entry_type="signal",
            slug="weather-file",
            label="Weather File",
            is_required=True,
        )
        output_entry = ValidatorCatalogEntryFactory(
            validator=validator,
            run_stage="output",
            entry_type="signal",
            slug="total-energy",
            label="Total Energy",
        )
        catalog = CatalogDisplay(
            entries=[input_entry, output_entry],
            inputs=[input_entry],
            outputs=[output_entry],
            input_derivations=[],
            output_derivations=[],
            input_total=1,
            output_total=1,
            uses_tabs=True,
        )
        step = WorkflowStepFactory(config={})

        result = build_unified_signals(catalog_display=catalog, step=step)

        assert result["has_inputs"] is True
        assert result["has_outputs"] is True
        assert len(result["input_signals"]) == 1
        assert len(result["output_signals"]) == 1

        assert result["input_signals"][0]["slug"] == "weather-file"
        assert result["input_signals"][0]["source"] == "catalog"
        assert result["output_signals"][0]["slug"] == "total-energy"
        assert result["output_signals"][0]["show_to_user"] is True

    def test_mixed_catalog_and_template_inputs(self):
        """Catalog INPUT entries and template variables merge into one input list.

        The merged list has catalog entries first, then template variables.
        This ensures the UI shows both sources under one "Inputs" tab.
        """
        from validibot.validations.models import CatalogDisplay
        from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
        from validibot.workflows.views_helpers import build_unified_signals

        validator = _make_energyplus_validator()
        input_entry = ValidatorCatalogEntryFactory(
            validator=validator,
            run_stage="input",
            entry_type="signal",
            slug="epw-file",
            label="EPW File",
            is_required=True,
        )
        catalog = CatalogDisplay(
            entries=[input_entry],
            inputs=[input_entry],
            outputs=[],
            input_derivations=[],
            output_derivations=[],
            input_total=1,
            output_total=0,
            uses_tabs=True,
        )
        step = WorkflowStepFactory(
            config={
                "template_variables": [
                    {"name": "U_FACTOR", "description": "U-Factor", "default": ""},
                ],
            },
        )

        result = build_unified_signals(catalog_display=catalog, step=step)

        expected_count = len(catalog.inputs) + len(step.config["template_variables"])
        assert len(result["input_signals"]) == expected_count
        # Catalog entries come first
        assert result["input_signals"][0]["source"] == "catalog"
        assert result["input_signals"][0]["slug"] == "epw-file"
        # Template variables follow
        assert result["input_signals"][1]["source"] == "template"
        assert result["input_signals"][1]["slug"] == "$U_FACTOR"

    def test_empty_step_produces_no_signals(self):
        """A step with no catalog entries and no template variables is empty.

        This happens for newly created steps before any config is set.
        """
        from validibot.workflows.views_helpers import build_unified_signals

        step = WorkflowStepFactory(config={})

        result = build_unified_signals(catalog_display=None, step=step)

        assert result["has_inputs"] is False
        assert result["has_outputs"] is False
        assert result["input_signals"] == []
        assert result["output_signals"] == []

    def test_output_display_signals_filtering(self):
        """Output signals respect the step's ``display_signals`` config.

        When ``display_signals`` is empty, all outputs show (backward
        compat).  When it lists specific slugs, only those are shown.
        """
        from validibot.validations.models import CatalogDisplay
        from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
        from validibot.workflows.views_helpers import build_unified_signals

        validator = _make_energyplus_validator()
        out1 = ValidatorCatalogEntryFactory(
            validator=validator,
            run_stage="output",
            entry_type="signal",
            slug="total-energy",
            label="Total Energy",
        )
        out2 = ValidatorCatalogEntryFactory(
            validator=validator,
            run_stage="output",
            entry_type="signal",
            slug="peak-load",
            label="Peak Load",
        )
        catalog = CatalogDisplay(
            entries=[out1, out2],
            inputs=[],
            outputs=[out1, out2],
            input_derivations=[],
            output_derivations=[],
            input_total=0,
            output_total=2,
            uses_tabs=True,
        )

        # With display_signals filtering to just one
        step = WorkflowStepFactory(
            config={"display_signals": ["total-energy"]},
        )
        result = build_unified_signals(catalog_display=catalog, step=step)

        shown = [s for s in result["output_signals"] if s["show_to_user"]]
        hidden = [s for s in result["output_signals"] if not s["show_to_user"]]
        assert len(shown) == 1
        assert shown[0]["slug"] == "total-energy"
        assert len(hidden) == 1
        assert hidden[0]["slug"] == "peak-load"

    def test_output_all_shown_when_display_signals_empty(self):
        """When ``display_signals`` is empty, all outputs are shown.

        This is the backward-compatible default — before the author
        configures signal visibility, everything is visible.
        """
        from validibot.validations.models import CatalogDisplay
        from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
        from validibot.workflows.views_helpers import build_unified_signals

        validator = _make_energyplus_validator()
        out1 = ValidatorCatalogEntryFactory(
            validator=validator,
            run_stage="output",
            entry_type="signal",
            slug="total-energy",
        )
        catalog = CatalogDisplay(
            entries=[out1],
            inputs=[],
            outputs=[out1],
            input_derivations=[],
            output_derivations=[],
            input_total=0,
            output_total=1,
            uses_tabs=True,
        )
        step = WorkflowStepFactory(config={})

        result = build_unified_signals(catalog_display=catalog, step=step)

        assert result["output_signals"][0]["show_to_user"] is True

    def test_input_derivations_included(self):
        """Input derivations from the catalog appear in the input list.

        Derivations are computed values that still need to be shown
        in the input stage — e.g. a derived ratio from two input signals.
        """
        from validibot.validations.models import CatalogDisplay
        from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
        from validibot.workflows.views_helpers import build_unified_signals

        validator = _make_energyplus_validator()
        derivation = ValidatorCatalogEntryFactory(
            validator=validator,
            run_stage="input",
            entry_type="derivation",
            slug="window-ratio",
            label="Window-to-Wall Ratio",
        )
        catalog = CatalogDisplay(
            entries=[derivation],
            inputs=[],
            outputs=[],
            input_derivations=[derivation],
            output_derivations=[],
            input_total=1,
            output_total=0,
            uses_tabs=True,
        )
        step = WorkflowStepFactory(config={})

        result = build_unified_signals(catalog_display=catalog, step=step)

        assert len(result["input_signals"]) == 1
        assert result["input_signals"][0]["slug"] == "window-ratio"
        assert result["input_signals"][0]["source"] == "catalog"
        assert result["input_signals"][0]["required"] is False


# ══════════════════════════════════════════════════════════════════════════════
# SingleTemplateVariableForm — per-variable modal editing
# ══════════════════════════════════════════════════════════════════════════════
# The per-variable edit form replaces the old "save all at once" annotation
# form.  Each template variable gets its own modal with this form.
# ══════════════════════════════════════════════════════════════════════════════


class TestSingleTemplateVariableForm:
    """Tests for ``SingleTemplateVariableForm`` — the per-variable edit form.

    This form is rendered in a modal when the user clicks "Edit" on a
    template-source input signal.  It populates initial values from the
    variable dict stored in ``step.config["template_variables"]``.
    """

    def test_initial_values_from_variable(self):
        """The form pre-populates fields from the variable dict.

        This ensures the modal shows the current annotations when opened,
        not blank fields.
        """
        from validibot.workflows.forms import SingleTemplateVariableForm

        variable = {
            "name": "U_FACTOR",
            "description": "U-Factor value",
            "default": "3.5",
            "units": "W/m2-K",
            "variable_type": "number",
            "min_value": 0.1,
            "min_exclusive": False,
            "max_value": 10.0,
            "max_exclusive": True,
            "choices": [],
        }

        form = SingleTemplateVariableForm(variable=variable)

        assert form.fields["description"].initial == "U-Factor value"
        assert form.fields["default"].initial == "3.5"
        assert form.fields["units"].initial == "W/m2-K"
        assert form.fields["variable_type"].initial == "number"
        assert form.fields["min_value"].initial == "0.1"
        assert form.fields["min_exclusive"].initial is False
        assert form.fields["max_value"].initial == "10.0"
        assert form.fields["max_exclusive"].initial is True
        assert form.fields["choices"].initial == ""

    def test_default_initial_values_without_variable(self):
        """Without a variable dict, the form has sensible defaults.

        This covers the edge case where the form is instantiated
        without context (shouldn't happen in practice but keeps
        the form robust).
        """
        from validibot.workflows.forms import SingleTemplateVariableForm

        form = SingleTemplateVariableForm()

        assert form.fields["variable_type"].initial == "text"
        assert form.fields["description"].initial is None

    def test_valid_submission(self):
        """A complete valid form submission passes validation.

        All fields are optional except ``variable_type`` which defaults
        to "text", so even minimal data should validate.
        """
        from validibot.workflows.forms import SingleTemplateVariableForm

        form = SingleTemplateVariableForm(
            data={
                "description": "Solar heat gain",
                "default": "0.4",
                "units": "",
                "variable_type": "number",
                "min_value": "0",
                "min_exclusive": False,
                "max_value": "1",
                "max_exclusive": False,
                "choices": "",
            },
        )

        assert form.is_valid()

    def test_minimal_submission_valid(self):
        """A submission with just the required radio field validates.

        This verifies that all text fields are truly optional — a user
        can open the modal, leave everything blank, and save.
        """
        from validibot.workflows.forms import SingleTemplateVariableForm

        form = SingleTemplateVariableForm(
            data={
                "description": "",
                "default": "",
                "units": "",
                "variable_type": "text",
                "min_value": "",
                "max_value": "",
                "choices": "",
            },
        )

        assert form.is_valid()

    def test_choices_initial_from_list(self):
        """A variable with choices gets them joined as newline-separated text.

        The form stores choices as a textarea, one per line.
        """
        from validibot.workflows.forms import SingleTemplateVariableForm

        variable = {
            "name": "GLAZING_TYPE",
            "choices": ["single", "double", "triple"],
        }

        form = SingleTemplateVariableForm(variable=variable)

        assert form.fields["choices"].initial == "single\ndouble\ntriple"


# ══════════════════════════════════════════════════════════════════════════════
# WorkflowStepTemplateVariableEditView — per-variable edit endpoint
# ══════════════════════════════════════════════════════════════════════════════
# The view handles GET (render modal content) and POST (save annotations
# for a single variable).  It's an HTMx endpoint that returns modal content
# or a 204 with refresh trigger.
# ══════════════════════════════════════════════════════════════════════════════


class TestTemplateVariableEditView:
    """Tests for ``WorkflowStepTemplateVariableEditView`` — the per-variable
    modal edit endpoint.

    GET returns the modal form content for a specific template variable.
    POST saves the annotations and triggers a page reload.
    """

    def _make_step_with_variables(self):
        """Create a workflow step with template variables for testing.

        Returns a (workflow, step) tuple.  Uses ``with_owner=True`` so
        the factory auto-creates an org membership for the workflow user.
        """
        workflow = WorkflowFactory(with_owner=True)
        step = WorkflowStepFactory(
            workflow=workflow,
            config={
                "template_variables": [
                    {
                        "name": "U_FACTOR",
                        "description": "U-Factor",
                        "default": "",
                        "units": "W/m2-K",
                        "variable_type": "number",
                    },
                    {
                        "name": "SHGC",
                        "description": "Solar Heat Gain",
                        "default": "0.4",
                        "units": "",
                        "variable_type": "text",
                    },
                ],
            },
        )
        return workflow, step

    def _login(self, client, workflow):
        """Log in as the workflow user with session org set."""
        client.force_login(workflow.user)
        session = client.session
        session["active_org_id"] = workflow.org_id
        session.save()

    def _url(self, workflow, step, var_index):
        """Build the template variable edit URL."""
        return (
            f"/app/workflows/{workflow.pk}"
            f"/steps/{step.pk}/template-variable/{var_index}/"
        )

    def test_get_renders_modal_content(self, client):
        """GET returns the modal form pre-populated with variable data.

        The response should contain the form fields and the variable's
        current name (which is immutable and shown as a header).
        """
        workflow, step = self._make_step_with_variables()
        self._login(client, workflow)

        response = client.get(self._url(workflow, step, 0))

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "U_FACTOR" in content

    def test_post_saves_single_variable(self, client):
        """POST updates only the targeted variable, preserving others.

        After saving, the variable at index 0 should have the new
        description and units, while the variable at index 1 is unchanged.
        """
        workflow, step = self._make_step_with_variables()
        self._login(client, workflow)

        response = client.post(
            self._url(workflow, step, 0),
            {
                "description": "Updated U-Factor Label",
                "default": "2.5",
                "units": "BTU/h-ft2-F",
                "variable_type": "number",
                "min_value": "0.1",
                "min_exclusive": "",
                "max_value": "10",
                "max_exclusive": "",
                "choices": "",
            },
        )

        assert response.status_code == HTTPStatus.NO_CONTENT

        # Verify the variable was updated
        step.refresh_from_db()
        tvars = step.config["template_variables"]
        assert tvars[0]["name"] == "U_FACTOR"  # Name preserved
        assert tvars[0]["description"] == "Updated U-Factor Label"
        assert tvars[0]["default"] == "2.5"
        assert tvars[0]["units"] == "BTU/h-ft2-F"

        # Second variable untouched
        assert tvars[1]["name"] == "SHGC"
        assert tvars[1]["description"] == "Solar Heat Gain"

    def test_invalid_index_returns_404(self, client):
        """Requesting a variable index beyond the list returns 404.

        This prevents index-out-of-range errors if the template
        variables change between when the page loaded and when the
        user clicks edit.
        """
        workflow, step = self._make_step_with_variables()
        self._login(client, workflow)

        response = client.get(self._url(workflow, step, 99))

        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_post_preserves_variable_name(self, client):
        """POST cannot change the variable name — it's immutable.

        The name comes from the IDF template and must remain stable
        for parameter substitution to work.  Even if a crafted POST
        tries to change it, the view ignores the form and uses the
        stored name.
        """
        workflow, step = self._make_step_with_variables()
        self._login(client, workflow)

        response = client.post(
            self._url(workflow, step, 0),
            {
                "description": "Hacked name attempt",
                "default": "",
                "units": "",
                "variable_type": "text",
                "min_value": "",
                "max_value": "",
                "choices": "",
            },
        )

        assert response.status_code == HTTPStatus.NO_CONTENT
        step.refresh_from_db()
        # Name is preserved from the original, not from form data
        assert step.config["template_variables"][0]["name"] == "U_FACTOR"


# ===========================================================================
# Phase 4: Launcher integration tests
# ===========================================================================
# These tests exercise `launch_energyplus_validation()` with real Django
# models (factories create actual DB rows) but mock the external I/O
# layer (GCS uploads, Cloud Run job triggering, callback URL building).
#
# Architecture note: Template preprocessing (parameter merging, validation,
# substitution) now happens *before* the launcher is called, in the shared
# `AdvancedValidator.validate()` → `EnergyPlusValidator.preprocess_
# submission()` pipeline.  By the time `launch_energyplus_validation()` is
# called, `submission.get_content()` already returns a fully resolved IDF.
# Template-specific tests (merge, validate, substitute) now live in
# `test_energyplus_preprocessing.py`.
#
# The launcher tests here verify the upload and job-trigger wiring only.
# ===========================================================================

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

        Template metadata is added by ``AdvancedValidator.validate()`` after
        preprocessing, not by the launcher.  Direct-mode submissions skip
        preprocessing entirely, so no template keys should appear.
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


class TestLauncherWithPreprocessedSubmission:
    """Tests for the launcher when the submission has been preprocessed.

    After the template preprocessing refactor, template resolution (parameter
    merging, validation, substitution) happens in
    ``EnergyPlusValidator.preprocess_submission()`` **before** the launcher
    is called.  By that point, ``submission.content`` has been set to the
    resolved IDF and ``submission.original_filename`` to ``resolved_model.idf``.

    These tests verify the launcher correctly handles such pre-processed
    submissions — uploading the resolved content with the proper file
    extension and MIME type.
    """

    def test_preprocessed_submission_uploads_resolved_idf(self):
        """When a submission has been preprocessed (content set to resolved IDF),
        the launcher uploads the resolved content with ``.idf`` extension.

        This verifies the launcher's filename-based extension detection works
        correctly with ``submission.original_filename = 'resolved_model.idf'``
        set by the preprocessing step.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        resolved_idf_content = (
            "Version,\n    24.2;\n\n"
            "WindowMaterial:SimpleGlazingSystem,\n"
            "    Glazing System,\n    2.5,\n    0.4;\n"
        )

        # Create fixtures WITHOUT a template resource — the launcher doesn't
        # need to know about templates.  Simulate preprocessing by setting
        # the submission content to the resolved IDF.
        fixtures = _make_launcher_fixtures(
            submission_content=resolved_idf_content,
        )

        # Simulate what preprocessing does: set original_filename to .idf
        fixtures["submission"].original_filename = "resolved_model.idf"
        fixtures["submission"].save(update_fields=["original_filename"])

        with _LauncherMocks() as mocks:
            result = launch_energyplus_validation(
                run=fixtures["run"],
                validator=fixtures["validator"],
                submission=fixtures["submission"],
                ruleset=None,
                step=fixtures["step"],
            )

        assert result.passed is None
        assert result.issues == []

        # The upload should use .idf extension and text/plain content type
        upload_call = mocks.upload_file.call_args
        uploaded_uri = upload_call.kwargs.get("uri", upload_call[1].get("uri", ""))
        uploaded_content_type = upload_call.kwargs.get(
            "content_type", upload_call[1].get("content_type", "")
        )
        uploaded_content = upload_call.kwargs.get(
            "content", upload_call[1].get("content", b"")
        ).decode("utf-8")

        assert uploaded_uri.endswith("/model.idf")
        assert uploaded_content_type == "text/plain"
        assert "2.5" in uploaded_content
        assert "0.4" in uploaded_content

    def test_preprocessed_submission_no_template_metadata_in_launcher_stats(self):
        """The launcher itself no longer produces template metadata in its stats.

        Template metadata (``template_parameters_used``, ``template_warnings``)
        is now added by ``AdvancedValidator.validate()`` after preprocessing
        completes, not by the launcher.  The launcher stats contain only job
        tracking info (job_name, execution_name, etc.).
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        fixtures = _make_launcher_fixtures(
            submission_content="Version,\n    24.2;\n",
        )
        fixtures["submission"].original_filename = "resolved_model.idf"
        fixtures["submission"].save(update_fields=["original_filename"])

        with _LauncherMocks():
            result = launch_energyplus_validation(
                run=fixtures["run"],
                validator=fixtures["validator"],
                submission=fixtures["submission"],
                ruleset=None,
                step=fixtures["step"],
            )

        # Launcher stats contain job info only — no template metadata
        assert "job_status" in result.stats
        assert "job_name" in result.stats
        assert "template_parameters_used" not in result.stats
        assert "template_warnings" not in result.stats
        assert "template_original_uri" not in result.stats
