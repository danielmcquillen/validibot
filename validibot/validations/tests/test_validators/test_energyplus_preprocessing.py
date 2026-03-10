"""
Tests for EnergyPlus submission preprocessing — template resolution.

The ``preprocess_energyplus_submission()`` function resolves parameterized
IDF templates into fully-resolved IDF files **before** execution dispatch.
This preprocessing happens in the shared ``AdvancedValidator.validate()``
pipeline, making template resolution platform-agnostic — it works
identically for Docker Compose, Cloud Run, and any future backend.

The function:
1. Detects template mode via ``MODEL_TEMPLATE`` step resource
2. Reads the template IDF from the step-owned resource file
3. Parses the submission as a flat JSON parameter dict
4. Merges submitter values with author defaults and validates constraints
5. Substitutes ``$VARIABLE`` placeholders into the template
6. Overwrites ``submission.content`` with the resolved IDF in-memory

These tests use real Django models (via factories) because preprocessing
reads from ``WorkflowStepResource`` rows and ``Submission`` objects.
The template merge/validate/substitute utilities are separately tested
in ``test_idf_template.py`` — these tests focus on the orchestration
and in-memory mutation of the submission.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.energyplus.preprocessing import (
    preprocess_energyplus_submission,
)
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.tests.factories import WorkflowStepResourceFactory

pytestmark = pytest.mark.django_db

# ---------------------------------------------------------------------------
# Shared template content
# ---------------------------------------------------------------------------

_TEMPLATE_IDF = """\
Version,
    24.2;  !- Version Identifier

WindowMaterial:SimpleGlazingSystem,
    Glazing System,          !- Name
    $U_FACTOR,               !- U-Factor {W/m2-K}
    $SHGC;                   !- Solar Heat Gain Coefficient
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_step_with_template(
    *,
    template_content: str = _TEMPLATE_IDF,
    step_config: dict | None = None,
) -> tuple:
    """Create a WorkflowStep with a MODEL_TEMPLATE resource.

    Returns (step, submission_factory_kwargs) — the caller creates
    the submission separately so it can control the content.
    """
    org = OrganizationFactory()
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    workflow = WorkflowFactory(org=org)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        config=step_config
        or {
            "template_variables": [
                {"name": "U_FACTOR", "variable_type": "number"},
                {"name": "SHGC", "variable_type": "number"},
            ],
            "case_sensitive": True,
        },
    )

    # Create the template resource (step-owned file)
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

    return step, {"workflow": workflow, "org": workflow.org, "project": None}


def _make_step_without_template(*, step_config: dict | None = None):
    """Create a WorkflowStep with NO template resource (direct mode)."""
    org = OrganizationFactory()
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    workflow = WorkflowFactory(org=org)
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        config=step_config or {},
    )
    return step, {"workflow": workflow, "org": workflow.org, "project": None}


# ===========================================================================
# Direct mode (no template)
# ===========================================================================
# When a step has no MODEL_TEMPLATE resource, the preprocessing function
# should be a no-op: return immediately with was_template=False and leave
# the submission untouched.
# ===========================================================================


class TestDirectModeNoOp:
    """Preprocessing is a no-op for direct-IDF submissions."""

    def test_returns_early_when_no_template_resource(self):
        """When the step has no MODEL_TEMPLATE resource, preprocessing
        returns immediately with ``was_template=False`` and an empty
        metadata dict.  The submission is not modified.
        """
        step, sub_kwargs = _make_step_without_template()
        submission = SubmissionFactory(content='{"some": "content"}', **sub_kwargs)

        result = preprocess_energyplus_submission(step=step, submission=submission)

        assert result.was_template is False
        assert result.template_metadata == {}
        # Submission content unchanged
        assert submission.get_content() == '{"some": "content"}'

    def test_original_filename_unchanged_for_direct_mode(self):
        """Direct-mode submissions retain their original filename.

        The preprocessing function should only modify the filename for
        template-mode submissions where it sets it to 'resolved_model.idf'.
        """
        step, sub_kwargs = _make_step_without_template()
        submission = SubmissionFactory(
            content="epjson content",
            original_filename="my_model.epjson",
            **sub_kwargs,
        )

        preprocess_energyplus_submission(step=step, submission=submission)

        assert submission.original_filename == "my_model.epjson"


# ===========================================================================
# Template mode — happy path
# ===========================================================================
# When a step has a MODEL_TEMPLATE resource, the preprocessing function
# reads the template, parses the JSON submission, merges parameters,
# substitutes placeholders, and overwrites submission.content in-place.
# ===========================================================================


class TestTemplateModeHappyPath:
    """Template resolution replaces submission content with resolved IDF."""

    def test_resolves_submission_content_to_idf(self):
        """The core happy-path test: submission.content is replaced with
        the resolved IDF where all $VARIABLE placeholders have been
        substituted with the submitter's parameter values.

        This is the fundamental contract of preprocessing — after this
        function runs, the submission looks identical to a direct-IDF
        upload.  No downstream consumer needs to know templates exist.
        """
        step, sub_kwargs = _make_step_with_template()
        params = json.dumps({"U_FACTOR": "2.5", "SHGC": "0.4"})
        submission = SubmissionFactory(content=params, **sub_kwargs)

        result = preprocess_energyplus_submission(step=step, submission=submission)

        # Template was detected and processed
        assert result.was_template is True

        # Submission content now contains the resolved IDF
        resolved = submission.get_content()
        assert "2.5" in resolved
        assert "0.4" in resolved
        assert "$U_FACTOR" not in resolved
        assert "$SHGC" not in resolved

    def test_original_filename_set_to_resolved_model_idf(self):
        """After template resolution, the submission's original_filename
        is set to 'resolved_model.idf' so that downstream consumers
        (like the launcher) use the correct file extension and MIME type.
        """
        step, sub_kwargs = _make_step_with_template()
        params = json.dumps({"U_FACTOR": "2.5", "SHGC": "0.4"})
        submission = SubmissionFactory(
            content=params,
            original_filename="params.json",
            **sub_kwargs,
        )

        preprocess_energyplus_submission(step=step, submission=submission)

        assert submission.original_filename == "resolved_model.idf"

    def test_returns_template_metadata(self):
        """The result includes metadata about which parameters were used
        and any validation warnings.  This metadata is merged into the
        step run's output stats by ``AdvancedValidator.validate()``.
        """
        step, sub_kwargs = _make_step_with_template()
        params = json.dumps({"U_FACTOR": "2.5", "SHGC": "0.4"})
        submission = SubmissionFactory(content=params, **sub_kwargs)

        result = preprocess_energyplus_submission(step=step, submission=submission)

        assert "template_parameters_used" in result.template_metadata
        assert result.template_metadata["template_parameters_used"] == {
            "U_FACTOR": "2.5",
            "SHGC": "0.4",
        }
        assert "template_warnings" in result.template_metadata
        assert isinstance(result.template_metadata["template_warnings"], list)

    def test_defaults_fill_missing_parameters(self):
        """When a template variable has a default value and the submitter
        omits it, the default is used in the resolved IDF.

        This verifies the integration with ``merge_and_validate_template_
        parameters()`` — author-defined defaults from the step config
        fill gaps in the submitter's JSON payload.
        """
        step, sub_kwargs = _make_step_with_template(
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
        # Only provide U_FACTOR; SHGC has a default
        params = json.dumps({"U_FACTOR": "3.0"})
        submission = SubmissionFactory(content=params, **sub_kwargs)

        result = preprocess_energyplus_submission(step=step, submission=submission)

        assert result.was_template is True
        assert result.template_metadata["template_parameters_used"]["SHGC"] == "0.25"
        assert result.template_metadata["template_parameters_used"]["U_FACTOR"] == "3.0"

        # Resolved IDF contains the default value
        resolved = submission.get_content()
        assert "3.0" in resolved
        assert "0.25" in resolved


# ===========================================================================
# Template mode — input validation
# ===========================================================================
# The preprocessing function validates the submission content before
# attempting template resolution.  Invalid input should raise
# ValidationError, which the caller (AdvancedValidator.validate())
# catches and converts to a user-friendly ValidationResult.
# ===========================================================================


class TestTemplateModeValidation:
    """Input validation during template preprocessing."""

    def test_non_json_submission_raises_validation_error(self):
        """If the submission content is not valid JSON, preprocessing
        raises ValidationError so the user gets a clear error message.
        """
        step, sub_kwargs = _make_step_with_template()
        submission = SubmissionFactory(content="not valid json!", **sub_kwargs)

        with pytest.raises(ValidationError, match="not valid JSON"):
            preprocess_energyplus_submission(step=step, submission=submission)

    def test_json_array_raises_validation_error(self):
        """Template parameters must be a flat JSON object, not an array.

        A JSON array (``[1, 2, 3]``) is valid JSON but not valid template
        parameters.  The error message should guide the user toward the
        expected format.
        """
        step, sub_kwargs = _make_step_with_template()
        submission = SubmissionFactory(content="[1, 2, 3]", **sub_kwargs)

        with pytest.raises(ValidationError, match="flat JSON object"):
            preprocess_energyplus_submission(step=step, submission=submission)

    def test_nested_objects_raises_validation_error(self):
        """Template parameters must be flat key-value pairs — nested
        objects and arrays are rejected.

        This prevents confusion when users accidentally nest parameters
        (e.g., ``{"glazing": {"U_FACTOR": "2.5"}}``).
        """
        step, sub_kwargs = _make_step_with_template()
        params = json.dumps({"U_FACTOR": "2.5", "nested": {"a": 1}})
        submission = SubmissionFactory(content=params, **sub_kwargs)

        with pytest.raises(ValidationError, match="Nested"):
            preprocess_energyplus_submission(step=step, submission=submission)

    def test_missing_required_param_raises_validation_error(self):
        """When a template variable has no default and the submitter
        doesn't provide it, merge validation raises ValidationError.

        This error propagates through preprocessing to the caller, which
        converts it to a user-friendly ValidationResult.
        """
        step, sub_kwargs = _make_step_with_template()
        # Empty params — both U_FACTOR and SHGC are required (no defaults)
        submission = SubmissionFactory(content="{}", **sub_kwargs)

        with pytest.raises(ValidationError):
            preprocess_energyplus_submission(step=step, submission=submission)


# ===========================================================================
# Template mode — encoding
# ===========================================================================
# Template files may use different text encodings.  The preprocessing
# function handles UTF-8 with a Latin-1 fallback, matching the upload
# validator's acceptance of Latin-1 encoded IDF files.
# ===========================================================================


class TestTemplateModeEncoding:
    """Template file encoding handling."""

    def test_latin1_template_decoded_successfully(self):
        """Template IDF files encoded in Latin-1 (ISO 8859-1) are handled
        correctly.  Latin-1 is common in older EnergyPlus files that
        contain accented characters in comments.

        The preprocessing function tries UTF-8 first, then falls back to
        Latin-1 — matching the upload validator's behavior.
        """
        # Create a template with a Latin-1 encoded character (e.g., ü = 0xFC)
        template_with_latin1 = (
            "Version,\n"
            "    24.2;  !- Versionsnümer\n\n"  # 'ü' is Latin-1
            "WindowMaterial:SimpleGlazingSystem,\n"
            "    Glazing System,\n"
            "    $U_FACTOR,\n"
            "    $SHGC;\n"
        )
        # Encode as Latin-1 bytes
        latin1_bytes = template_with_latin1.encode("latin-1")

        org = OrganizationFactory()
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        workflow = WorkflowFactory(org=org)
        step = WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            config={
                "template_variables": [
                    {"name": "U_FACTOR", "variable_type": "number"},
                    {"name": "SHGC", "variable_type": "number"},
                ],
                "case_sensitive": True,
            },
        )

        WorkflowStepResourceFactory(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            validator_resource_file=None,
            step_resource_file=SimpleUploadedFile(
                "template_latin1.idf",
                latin1_bytes,
                content_type="text/plain",
            ),
            filename="template_latin1.idf",
            resource_type="energyplus_model_template",
        )

        params = json.dumps({"U_FACTOR": "2.5", "SHGC": "0.4"})
        submission = SubmissionFactory(
            content=params,
            workflow=workflow,
            org=workflow.org,
            project=None,
        )

        result = preprocess_energyplus_submission(step=step, submission=submission)

        assert result.was_template is True
        resolved = submission.get_content()
        assert "2.5" in resolved
        assert "0.4" in resolved


# ===========================================================================
# Template mode — error type contract
# ===========================================================================
# _read_template_content() must raise ValidationError (not ValueError)
# because the caller (AdvancedValidator.validate()) only catches
# ValidationError.  A ValueError would escape as an unhandled 500.
# ===========================================================================


class TestReadTemplateContentErrorType:
    """Verify that template I/O errors raise ValidationError."""

    def test_undecoded_template_raises_validation_error_not_value_error(self):
        """When a template file contains bytes that cannot be decoded
        as either UTF-8 or Latin-1, ``_read_template_content()`` must
        raise ``ValidationError`` (not ``ValueError``).

        This is important because the caller's exception handler
        (``AdvancedValidator.validate()``) only catches
        ``ValidationError``.  A ``ValueError`` would propagate as an
        unhandled 500 error to the user.
        """
        from validibot.validations.validators.energyplus.preprocessing import (
            _read_template_content,
        )

        # Create a mock resource whose file returns bytes that
        # decode_idf_bytes() cannot decode (returns None).
        mock_resource = MagicMock()
        # Pure-null bytes that can't be valid UTF-8 or Latin-1 text
        # (decode_idf_bytes returns None when both decoders fail).
        # In practice, Latin-1 decodes everything, so we mock at
        # a higher level.
        mock_resource.step_resource_file.read.return_value = b"\x00content"
        mock_resource.step_resource_file.seek = MagicMock()

        # Patch decode_idf_bytes to simulate decode failure
        with (
            patch(
                "validibot.validations.validators.energyplus.preprocessing.decode_idf_bytes",
                return_value=(None, False),
            ),
            pytest.raises(ValidationError, match="could not be decoded"),
        ):
            _read_template_content(mock_resource)

    def test_io_error_raises_validation_error(self):
        """When the storage backend fails to read the template file
        (e.g., file deleted, GCS unavailable), ``_read_template_content()``
        must raise ``ValidationError`` with a user-friendly message.

        This catches IOError/OSError from the storage backend and
        wraps it so the caller's ``except ValidationError`` handler
        can produce a clean user-facing error message.
        """
        from validibot.validations.validators.energyplus.preprocessing import (
            _read_template_content,
        )

        mock_resource = MagicMock()
        mock_resource.step_resource_file.read.side_effect = OSError(
            "File not found in storage"
        )

        with pytest.raises(ValidationError, match="Could not read template file"):
            _read_template_content(mock_resource)
