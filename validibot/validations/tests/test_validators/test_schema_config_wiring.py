"""Regression tests: JSON/XML Schema edit forms must prefill the FULL schema.

The step-config edit forms prefill their schema textarea so the author can
see and tweak the current schema. That prefill must come from the Ruleset's
full stored source (``Ruleset.rules``) — NEVER from the step's
``schema_text_preview`` display value, which views_helpers truncates to the
first 1,200 characters for the step summary card. Browsers resubmit
prefilled textarea content verbatim, and the forms treat any non-empty text
as a new "text" schema source, so a truncated prefill silently replaces any
schema longer than the cutoff with its own first 1,200 characters the next
time the edit form is saved untouched.

The same bug was fixed for ``SchematronStepConfigForm`` first (see
``test_schematron_config_wiring.py``); these tests pin the identical
guarantee for the JSON Schema and XML Schema step forms:

- the edit form prefills the textarea with the step's FULL schema, for both
  pasted-text and uploaded-file steps;
- blindly resubmitting that prefill round-trips the stored schema
  losslessly.
"""

from __future__ import annotations

import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.models import Ruleset
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.forms import JsonSchemaStepConfigForm
from validibot.workflows.forms import XmlSchemaStepConfigForm
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.views_helpers import save_workflow_step

# The display bucket stores only the first 1,200 chars of the schema
# (``schema_text_preview`` in views_helpers). The fixtures below are
# deliberately longer, so a prefill sourced from the preview could never
# masquerade as the full schema in these tests.
DISPLAY_PREVIEW_MAX_CHARS = 1200


def _long_json_schema() -> str:
    """Build a valid Draft 2020-12 schema longer than the preview cutoff."""
    properties = {
        f"field_{index:03d}": {
            "type": "string",
            "description": (
                f"Synthetic property {index} padding the schema well past "
                "the 1,200-character display-preview cutoff."
            ),
        }
        for index in range(24)
    }
    return json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": properties,
        },
        indent=2,
    )


def _long_xsd_schema() -> str:
    """Build a valid XSD longer than the preview cutoff."""
    elements = "\n".join(
        f'  <xs:element name="field{index:03d}" type="xs:string"/>'
        for index in range(30)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
        f"{elements}\n"
        "</xs:schema>"
    )


JSON_SCHEMA_TEXT = _long_json_schema()
XSD_SCHEMA_TEXT = _long_xsd_schema()


def _stored_rules(step) -> str:
    """Read the step's rules back from the database, not the instance cache.

    The losslessness claim is about what a future edit form (or validation
    run) will actually load, so the assertions must go through a fresh DB
    read rather than trusting whatever instance the save flow mutated in
    memory.
    """
    return Ruleset.objects.get(pk=step.ruleset_id).rules


# ── JSON Schema step form ────────────────────────────────────────────────────
# Create → edit → blind re-save round trips, for both authoring paths
# (pasted text and uploaded file).


@pytest.mark.django_db
class TestJsonSchemaEditPrefill:
    def _create_step(self, form: JsonSchemaStepConfigForm):
        workflow = WorkflowFactory()
        validator = ValidatorFactory(
            validation_type=ValidationType.JSON_SCHEMA,
            supports_assertions=True,
        )
        assert form.is_valid(), form.errors
        return workflow, validator, save_workflow_step(workflow, validator, form)

    def test_edit_form_prefills_the_full_schema(self):
        """The edit textarea shows the FULL schema, not the truncated preview.

        The regression this pins: the form used to prefill from
        ``display_settings["schema_text_preview"]``, so any schema longer
        than 1,200 chars rendered truncated — and, because prefilled text is
        resubmitted verbatim, was truncated in storage on the next save.
        """
        _, _, step = self._create_step(
            JsonSchemaStepConfigForm(
                data={"name": "Long schema", "schema_text": JSON_SCHEMA_TEXT},
            ),
        )
        # Precondition: the display preview really is truncated — otherwise
        # this test could pass even with a preview-sourced prefill.
        preview = step.display_settings["schema_text_preview"]
        assert len(preview) == DISPLAY_PREVIEW_MAX_CHARS
        assert len(step.ruleset.rules) > DISPLAY_PREVIEW_MAX_CHARS

        prefilled = JsonSchemaStepConfigForm(step=step).fields["schema_text"].initial

        assert prefilled == step.ruleset.rules
        assert len(prefilled) > DISPLAY_PREVIEW_MAX_CHARS

    def test_resaving_the_prefilled_schema_is_lossless(self):
        """Submitting the edit form untouched keeps the pasted schema intact.

        The browser resubmits whatever the textarea was prefilled with, so
        saving an otherwise-untouched edit form must round-trip the full
        schema byte-for-byte — this is why the prefill comes from
        ``Ruleset.rules`` and never from ``schema_text_preview``.
        """
        workflow, validator, step = self._create_step(
            JsonSchemaStepConfigForm(
                data={"name": "Long schema", "schema_text": JSON_SCHEMA_TEXT},
            ),
        )
        rules_before = _stored_rules(step)
        assert len(rules_before) > DISPLAY_PREVIEW_MAX_CHARS

        prefilled = JsonSchemaStepConfigForm(step=step).fields["schema_text"].initial
        edit_form = JsonSchemaStepConfigForm(
            data={"name": "Long schema", "schema_text": prefilled},
            step=step,
        )
        assert edit_form.is_valid(), edit_form.errors
        step = save_workflow_step(workflow, validator, edit_form, step=step)

        assert _stored_rules(step) == rules_before

    def test_resaving_an_uploaded_schema_is_lossless(self):
        """A blind re-save also preserves schemas that arrived as uploads.

        Uploaded schemas are stored on ``Ruleset.rules_file`` rather than
        inline, so the prefill exercises the file-reading branch of
        ``Ruleset.rules``. Resubmitting that prefill re-stores the same
        content (as inline text — the storage location may change, the
        schema content must not).
        """
        workflow, validator, step = self._create_step(
            JsonSchemaStepConfigForm(
                data={"name": "Uploaded schema"},
                files={
                    "schema_file": SimpleUploadedFile(
                        "schema.json",
                        JSON_SCHEMA_TEXT.encode("utf-8"),
                    ),
                },
            ),
        )
        assert _stored_rules(step) == JSON_SCHEMA_TEXT

        prefilled = JsonSchemaStepConfigForm(step=step).fields["schema_text"].initial
        assert prefilled == JSON_SCHEMA_TEXT
        edit_form = JsonSchemaStepConfigForm(
            data={"name": "Uploaded schema", "schema_text": prefilled},
            step=step,
        )
        assert edit_form.is_valid(), edit_form.errors
        step = save_workflow_step(workflow, validator, edit_form, step=step)

        assert _stored_rules(step) == JSON_SCHEMA_TEXT


# ── XML Schema step form ─────────────────────────────────────────────────────
# The same guarantees for the XSD form, which shares the preview-based
# summary card and therefore had the same truncated-prefill bug.


@pytest.mark.django_db
class TestXmlSchemaEditPrefill:
    def _create_step(self, form: XmlSchemaStepConfigForm):
        workflow = WorkflowFactory()
        validator = ValidatorFactory(
            validation_type=ValidationType.XML_SCHEMA,
            supports_assertions=True,
        )
        assert form.is_valid(), form.errors
        return workflow, validator, save_workflow_step(workflow, validator, form)

    def test_edit_form_prefills_the_full_schema(self):
        """The edit textarea shows the FULL XSD, not the truncated preview."""
        _, _, step = self._create_step(
            XmlSchemaStepConfigForm(
                data={
                    "name": "Long XSD",
                    "schema_type": XMLSchemaType.XSD.value,
                    "schema_text": XSD_SCHEMA_TEXT,
                },
            ),
        )
        preview = step.display_settings["schema_text_preview"]
        assert len(preview) == DISPLAY_PREVIEW_MAX_CHARS
        assert len(step.ruleset.rules) > DISPLAY_PREVIEW_MAX_CHARS

        prefilled = XmlSchemaStepConfigForm(step=step).fields["schema_text"].initial

        assert prefilled == step.ruleset.rules
        assert len(prefilled) > DISPLAY_PREVIEW_MAX_CHARS

    def test_resaving_the_prefilled_schema_is_lossless(self):
        """Submitting the edit form untouched keeps the pasted XSD intact."""
        workflow, validator, step = self._create_step(
            XmlSchemaStepConfigForm(
                data={
                    "name": "Long XSD",
                    "schema_type": XMLSchemaType.XSD.value,
                    "schema_text": XSD_SCHEMA_TEXT,
                },
            ),
        )
        rules_before = _stored_rules(step)
        assert len(rules_before) > DISPLAY_PREVIEW_MAX_CHARS

        prefilled = XmlSchemaStepConfigForm(step=step).fields["schema_text"].initial
        edit_form = XmlSchemaStepConfigForm(
            data={
                "name": "Long XSD",
                "schema_type": XMLSchemaType.XSD.value,
                "schema_text": prefilled,
            },
            step=step,
        )
        assert edit_form.is_valid(), edit_form.errors
        step = save_workflow_step(workflow, validator, edit_form, step=step)

        assert _stored_rules(step) == rules_before

    def test_resaving_an_uploaded_schema_is_lossless(self):
        """A blind re-save also preserves XSDs that arrived as uploads."""
        workflow, validator, step = self._create_step(
            XmlSchemaStepConfigForm(
                data={
                    "name": "Uploaded XSD",
                    "schema_type": XMLSchemaType.XSD.value,
                },
                files={
                    "schema_file": SimpleUploadedFile(
                        "schema.xsd",
                        XSD_SCHEMA_TEXT.encode("utf-8"),
                    ),
                },
            ),
        )
        assert _stored_rules(step) == XSD_SCHEMA_TEXT

        prefilled = XmlSchemaStepConfigForm(step=step).fields["schema_text"].initial
        assert prefilled == XSD_SCHEMA_TEXT
        edit_form = XmlSchemaStepConfigForm(
            data={
                "name": "Uploaded XSD",
                "schema_type": XMLSchemaType.XSD.value,
                "schema_text": prefilled,
            },
            step=step,
        )
        assert edit_form.is_valid(), edit_form.errors
        step = save_workflow_step(workflow, validator, edit_form, step=step)

        assert _stored_rules(step) == XSD_SCHEMA_TEXT
