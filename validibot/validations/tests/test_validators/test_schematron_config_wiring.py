"""Tests for Schematron step authoring: form, Ruleset storage, save flow.

Under the upload model (ADR-2026-07-01, revised D5) a Schematron step is
authored exactly like an XML Schema step: the author pastes or uploads
their rules, the step's Ruleset stores the source, and the existing
ruleset-immutability gate protects locked workflows. These tests pin that
authoring surface:

- ``SchematronStepConfigForm`` accepts pasted/uploaded ``.sch``, keeps the
  saved rules on blank edits, and rejects non-Schematron content at upload.
- ``Ruleset.clean()`` requires rules content on SCHEMATRON rulesets (the
  schema-ruleset rule).
- ``save_workflow_step`` stores the rules with a sha256 provenance stamp
  and the optional D10 documentation-URL template.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.workflows.forms import SchematronStepConfigForm
from validibot.workflows.forms import get_config_form_class

ASSETS = Path("tests/assets/schematron")
SCH_TEXT = (ASSETS / "en16931_subset.sch").read_text()
DOC_URL_TEMPLATE = "https://docs.example.test/rules/#{rule_id}"


# ── The step-config form ─────────────────────────────────────────────────────


class TestSchematronStepConfigForm:
    def test_pasted_rules_validate_and_carry_the_payload(self):
        """Pasted .sch text is accepted and normalised into the payload.

        ``schematron_payload`` is what the builder persists — producing it
        in clean() means text and upload paths converge on one code path.
        """
        form = SchematronStepConfigForm(
            data={"name": "EN 16931 rules", "schematron_text": SCH_TEXT},
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["schematron_source"] == "text"
        assert form.cleaned_data["schematron_payload"] == SCH_TEXT.strip()

    def test_uploaded_file_validates_and_carries_the_payload(self):
        """An uploaded .sch file is decoded and accepted like pasted text."""
        form = SchematronStepConfigForm(
            data={"name": "EN 16931 rules"},
            files={
                "schematron_file": SimpleUploadedFile(
                    "rules.sch",
                    SCH_TEXT.encode("utf-8"),
                ),
            },
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["schematron_source"] == "upload"
        assert "VB-CO-15" in form.cleaned_data["schematron_payload"]

    def test_non_schematron_content_is_rejected_at_upload(self):
        """A random XML document fails with an author-facing message.

        Catching "wrong file" at authoring time (root element must be the
        ISO Schematron <schema>) beats a run-time engine error — the author
        is right there to fix it.
        """
        form = SchematronStepConfigForm(
            data={"name": "Rules", "schematron_text": "<invoice/>"},
        )
        assert not form.is_valid()
        assert "schematron_text" in form.errors

    def test_blank_fields_require_rules_for_a_new_step(self):
        """A new step with neither text nor file gets a clear error."""
        form = SchematronStepConfigForm(data={"name": "Rules"})
        assert not form.is_valid()
        assert "schematron_text" in form.errors
        assert "schematron_file" in form.errors

    def test_schematron_maps_to_its_form(self):
        """get_config_form_class routes SCHEMATRON to the upload form."""
        assert (
            get_config_form_class(ValidationType.SCHEMATRON) is SchematronStepConfigForm
        )


# ── Ruleset.clean(): rules content required, like any schema ruleset ────────


@pytest.mark.django_db
class TestSchematronRulesetClean:
    def test_ruleset_with_rules_text_passes(self):
        """A SCHEMATRON ruleset carrying .sch source is valid."""
        ruleset = Ruleset(
            org=OrganizationFactory(),
            name="step-rules",
            ruleset_type=RulesetType.SCHEMATRON,
            version="1",
            rules_text=SCH_TEXT,
        )
        ruleset.full_clean()  # must not raise

    def test_ruleset_without_content_is_rejected(self):
        """A SCHEMATRON ruleset with no rules is rejected.

        Same rule as JSON/XML schema rulesets: the ruleset IS the rules;
        an empty one can only produce meaningless runs.
        """
        ruleset = Ruleset(
            org=OrganizationFactory(),
            name="step-rules",
            ruleset_type=RulesetType.SCHEMATRON,
            version="1",
        )
        with pytest.raises(ValidationError, match="rules"):
            ruleset.full_clean()


# ── save_workflow_step: the full authoring flow ──────────────────────────────


@pytest.mark.django_db
class TestSaveWorkflowStep:
    def test_saving_a_step_stores_rules_with_provenance(self):
        """Saving a Schematron step persists rules + sha256 + doc template.

        The three durable artefacts of authoring: the source on the step's
        org-owned ruleset (deep-copied on workflow versioning, frozen by
        the immutability gate once locked), its sha256 (the provenance
        identity the container echoes back), and the optional D10 deep-link
        template.
        """
        from validibot.validations.tests.factories import ValidatorFactory
        from validibot.workflows.tests.factories import WorkflowFactory
        from validibot.workflows.views_helpers import save_workflow_step

        workflow = WorkflowFactory()
        validator = ValidatorFactory(
            validation_type=ValidationType.SCHEMATRON,
            supports_assertions=True,
        )
        form = SchematronStepConfigForm(
            data={
                "name": "EN 16931 rules",
                "schematron_text": SCH_TEXT,
                "rule_doc_url_template": DOC_URL_TEMPLATE,
            },
        )
        assert form.is_valid(), form.errors

        step = save_workflow_step(workflow, validator, form)

        assert step.ruleset is not None
        assert step.ruleset.org == workflow.org
        assert step.ruleset.ruleset_type == RulesetType.SCHEMATRON
        assert "VB-CO-15" in step.ruleset.rules
        expected_sha = hashlib.sha256(
            step.ruleset.rules_text.encode("utf-8"),
        ).hexdigest()
        assert step.ruleset.metadata["schematron_sha256"] == expected_sha
        assert step.ruleset.metadata["rule_doc_url_template"] == DOC_URL_TEMPLATE
        # The preview is cosmetic — it must land in display_settings, never
        # the hashed semantic bucket.
        assert "schematron_preview" in step.display_settings
        assert step.config == {}

    def test_blank_edit_keeps_the_saved_rules(self):
        """Re-saving with blank rule fields preserves the stored source.

        The XSD "keep" behaviour: editing a step's name must not force the
        author to re-upload their rules.
        """
        from validibot.validations.tests.factories import ValidatorFactory
        from validibot.workflows.tests.factories import WorkflowFactory
        from validibot.workflows.views_helpers import save_workflow_step

        workflow = WorkflowFactory()
        validator = ValidatorFactory(
            validation_type=ValidationType.SCHEMATRON,
            supports_assertions=True,
        )
        create_form = SchematronStepConfigForm(
            data={"name": "Rules", "schematron_text": SCH_TEXT},
        )
        assert create_form.is_valid(), create_form.errors
        step = save_workflow_step(workflow, validator, create_form)
        original_rules = step.ruleset.rules

        edit_form = SchematronStepConfigForm(
            data={"name": "Renamed step"},
            step=step,
        )
        assert edit_form.is_valid(), edit_form.errors
        step = save_workflow_step(workflow, validator, edit_form, step=step)

        assert step.name == "Renamed step"
        assert step.ruleset.rules == original_rules
