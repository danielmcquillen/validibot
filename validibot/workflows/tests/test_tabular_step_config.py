"""
Tests for the Tabular Validator's step settings editor — the form and the
config builder that persist a tabular step's configuration.

### What this suite covers and why

The settings editor is how an author configures a tabular step through the UI.
Two pieces carry the logic:

- ``TabularStepConfigForm`` validates the dialect and the column schema, which
  can be supplied by pasting a Frictionless descriptor **or** uploading a sample
  to infer one. It must accept valid input, reject malformed descriptors, and
  require *some* schema source on a brand-new step.
- ``build_tabular_config`` writes the descriptor to ``ruleset.rules_text`` and
  the dialect to ``ruleset.metadata`` — the exact two places the
  ``TabularValidator`` reads at run time — so a saved config round-trips into a
  schema the validator can enforce.

Form tests need no database (step is ``None``); the builder tests use factories.
"""

from __future__ import annotations

import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test import SimpleTestCase
from django.test import TestCase
from django.urls import reverse

from validibot.submissions.constants import SubmissionFileType
from validibot.users.constants import RoleCode
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.tabular.schema import parse_table_schema
from validibot.workflows.forms import TabularStepConfigForm
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.views_helpers import build_tabular_config


def _login_as_author(client: Client, workflow) -> None:
    """Log in as the workflow owner with author permissions in the org."""
    membership = workflow.user.memberships.get(org=workflow.org)
    membership.set_roles({RoleCode.AUTHOR})
    workflow.user.set_current_org(workflow.org)
    client.force_login(workflow.user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()


_DESCRIPTOR = json.dumps(
    {
        "fields": [
            {"name": "lat", "type": "number"},
            {"name": "lon", "type": "number"},
        ],
    },
)


class TabularStepConfigFormTests(SimpleTestCase):
    """Form validation of the dialect + schema-acquisition paths."""

    def test_pasted_descriptor_validates_and_is_tagged_text(self):
        """A valid pasted Table Schema descriptor validates, and the cleaned
        data carries both the raw JSON (for storage) and the parsed dict (for
        the column count), tagged as the ``text`` source.
        """
        form = TabularStepConfigForm(
            data={
                "name": "Check submission",
                "table_schema": _DESCRIPTOR,
                "encoding": "utf-8",
                "delimiter": "",
                "has_header": "on",
            },
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(form.cleaned_data["schema_source"], "text")
        self.assertEqual(form.cleaned_data["descriptor_json"], _DESCRIPTOR)
        self.assertEqual(len(form.cleaned_data["descriptor"]["fields"]), 2)

    def test_invalid_descriptor_json_is_rejected(self):
        """A descriptor that isn't valid JSON is a field error, not a crash —
        the author sees what's wrong rather than a 500 on save.
        """
        form = TabularStepConfigForm(
            data={"name": "x", "table_schema": "{ not valid json"},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("table_schema", form.errors)

    def test_descriptor_without_fields_is_rejected(self):
        """A JSON object that isn't a usable Table Schema (no ``fields``) is
        rejected — the parser's contract surfaces as a form error.
        """
        form = TabularStepConfigForm(
            data={"name": "x", "table_schema": json.dumps({"primaryKey": "id"})},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("table_schema", form.errors)

    def test_sample_upload_infers_descriptor(self):
        """Uploading a sample CSV infers a descriptor (tagged ``infer``), typed
        from the sample's values — the no-coding setup path.
        """
        sample = SimpleUploadedFile(
            "sample.csv",
            b"lat,lon\n10,20\n-5,30\n",
            content_type="text/csv",
        )
        form = TabularStepConfigForm(
            data={"name": "x", "encoding": "utf-8", "has_header": "on"},
            files={"sample_file": sample},
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(form.cleaned_data["schema_source"], "infer")
        types = {
            field["name"]: field["type"]
            for field in form.cleaned_data["descriptor"]["fields"]
        }
        self.assertEqual(types, {"lat": "integer", "lon": "integer"})

    def test_paste_and_sample_together_is_rejected(self):
        """Providing both a pasted descriptor and a sample is ambiguous and
        rejected — the author must choose one source.
        """
        sample = SimpleUploadedFile("s.csv", b"a\n1\n", content_type="text/csv")
        form = TabularStepConfigForm(
            data={"name": "x", "table_schema": _DESCRIPTOR},
            files={"sample_file": sample},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("table_schema", form.errors)

    def test_new_step_requires_a_schema_source(self):
        """A brand-new step with neither a descriptor nor a sample is rejected —
        a tabular step is meaningless without a schema.
        """
        form = TabularStepConfigForm(data={"name": "x"})
        self.assertFalse(form.is_valid())
        self.assertIn("table_schema", form.errors)


class BuildTabularConfigTests(TestCase):
    """The builder writes the descriptor + dialect to the ruleset."""

    def _tabular_step(self):
        workflow = WorkflowFactory()
        validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            supports_assertions=True,
        )
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        return workflow, step

    def test_persists_descriptor_to_rules_text_and_dialect_to_metadata(self):
        """A validated form is written to the ruleset: the descriptor into
        ``rules_text`` and dialect into ``metadata`` — the round-trip the
        validator reads back at run time.
        """
        workflow, step = self._tabular_step()
        form = TabularStepConfigForm(
            data={
                "name": "x",
                "table_schema": _DESCRIPTOR,
                "encoding": "utf-8",
                "delimiter": ",",
                "has_header": "on",
            },
            step=step,
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())

        config, ruleset = build_tabular_config(workflow, form, step)

        self.assertEqual(ruleset.rules_text, _DESCRIPTOR)
        self.assertEqual(ruleset.metadata["delimiter"], ",")
        self.assertEqual(ruleset.metadata["encoding"], "utf-8")
        self.assertTrue(ruleset.metadata["has_header"] is True)
        self.assertEqual(config["schema_source"], "text")
        self.assertEqual(config["column_count"], 2)
        # The stored descriptor parses into a schema the validator can enforce.
        schema = parse_table_schema(json.loads(ruleset.rules_text))
        self.assertEqual(schema.field_names(), ["lat", "lon"])

    def test_keep_source_updates_dialect_without_touching_schema(self):
        """Editing dialect-only on an existing step (no new schema) keeps the
        stored descriptor and just updates the dialect — so an author can
        tweak the delimiter without re-pasting the schema.
        """
        workflow, step = self._tabular_step()
        # First save establishes the schema.
        first_form = TabularStepConfigForm(
            data={
                "name": "x",
                "table_schema": _DESCRIPTOR,
                "encoding": "utf-8",
                "delimiter": ",",
                "has_header": "on",
            },
            step=step,
        )
        self.assertTrue(first_form.is_valid(), first_form.errors.as_json())
        config, ruleset = build_tabular_config(workflow, first_form, step)
        step.ruleset = ruleset
        step.config = config
        step.save(update_fields=["ruleset", "config"])

        # Second save changes only the delimiter; no schema provided → "keep".
        keep_form = TabularStepConfigForm(
            data={
                "name": "x",
                "encoding": "utf-8",
                "delimiter": ";",
                "has_header": "on",
            },
            step=step,
        )
        self.assertTrue(keep_form.is_valid(), keep_form.errors.as_json())
        keep_config, keep_ruleset = build_tabular_config(workflow, keep_form, step)

        self.assertEqual(keep_config["schema_source"], "keep")
        self.assertEqual(keep_ruleset.rules_text, _DESCRIPTOR)  # unchanged
        self.assertEqual(keep_ruleset.metadata["delimiter"], ";")  # updated


class TabularStepSettingsViewTests(TestCase):
    """End-to-end: the settings page renders the tabular form and saves it.

    These prove the wiring through the real request path —
    ``get_config_form_class`` returns the tabular form, and the
    ``save_workflow_step`` dispatch routes to ``build_tabular_config`` — not
    just the form/builder in isolation.
    """

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def _tabular_workflow_and_step(self):
        # Tabular submissions are plain text, so the workflow must allow TEXT
        # or the settings POST fails its validator-compatibility check.
        workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.TEXT])
        validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            supports_assertions=True,
        )
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        return workflow, step

    def _settings_url(self, workflow, step):
        return reverse(
            "workflows:workflow_step_settings",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )

    def test_settings_page_renders_tabular_form(self):
        """GET on a tabular step's settings page renders the tabular config
        form (delimiter + Table Schema fields), confirming
        ``get_config_form_class`` selects ``TabularStepConfigForm``.
        """
        workflow, step = self._tabular_workflow_and_step()
        _login_as_author(self.client, workflow)
        response = self.client.get(self._settings_url(workflow, step))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Table Schema")
        self.assertContains(response, "Delimiter")

    def test_settings_post_saves_descriptor_to_ruleset(self):
        """POSTing a valid tabular config persists the descriptor to the
        step's ruleset — the full author flow, through the view dispatch.
        """
        workflow, step = self._tabular_workflow_and_step()
        _login_as_author(self.client, workflow)
        response = self.client.post(
            self._settings_url(workflow, step),
            data={
                "name": "Validate submission CSV",
                "table_schema": _DESCRIPTOR,
                "encoding": "utf-8",
                "delimiter": ",",
                "has_header": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        step.refresh_from_db()
        self.assertIsNotNone(step.ruleset)
        self.assertEqual(step.ruleset.rules_text, _DESCRIPTOR)
        self.assertEqual(step.ruleset.metadata["delimiter"], ",")
        self.assertEqual(step.typed_config.column_count, 2)
