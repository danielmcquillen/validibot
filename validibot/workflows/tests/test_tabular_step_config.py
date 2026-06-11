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
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
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


def _column_formset_data(
    columns: list[dict[str, object]],
    *,
    base_descriptor: dict | None = None,
) -> dict[str, object]:
    """Build browser-shaped formset POST data for column-editor tests."""
    data: dict[str, object] = {
        "columns-TOTAL_FORMS": str(len(columns)),
        "columns-INITIAL_FORMS": str(len(columns)),
        "columns-MIN_NUM_FORMS": "1",
        "columns-MAX_NUM_FORMS": "1000",
        "schema_base": json.dumps(base_descriptor or {}),
    }
    for index, column in enumerate(columns):
        prefix = f"columns-{index}"
        data[f"{prefix}-original_name"] = column.get("original_name", "")
        data[f"{prefix}-name"] = column.get("name", "")
        data[f"{prefix}-type"] = column.get("type", "string")
        data[f"{prefix}-ORDER"] = str(column.get("order", index + 1))
        data[f"{prefix}-required_when_present"] = column.get(
            "required_when_present",
            "",
        )
        for boolean_field in ("required", "unique", "primary_key", "DELETE"):
            if column.get(boolean_field):
                data[f"{prefix}-{boolean_field}"] = "on"
        for field_name in (
            "minimum",
            "maximum",
            "min_length",
            "max_length",
            "pattern",
            "enum_values",
        ):
            data[f"{prefix}-{field_name}"] = column.get(field_name, "")
    return data


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

    def test_duplicate_field_descriptor_is_rejected(self):
        """A pasted descriptor with duplicate field names is a field error, not
        a deferred crash. The form surfaces the schema parser's ValueError, so
        the author fixes it at save time rather than the validator blowing up on
        a headerless read (the P1 regression, caught one layer earlier).
        """
        form = TabularStepConfigForm(
            data={
                "name": "x",
                "table_schema": json.dumps(
                    {"fields": [{"name": "lat"}, {"name": "lat"}]},
                ),
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("table_schema", form.errors)

    def test_encoding_is_not_an_editable_field(self):
        """Encoding is intentionally absent in V1. Submitted content reaches the
        validator already decoded as UTF-8, so a per-step encoding setting would
        silently corrupt non-UTF-8 input — an honest no-field beats a field that
        lies. The dialect is pinned to UTF-8 end-to-end.
        """
        form = TabularStepConfigForm(data={"name": "x"})
        self.assertNotIn("encoding", form.fields)

    def test_column_editor_builds_structured_descriptor(self):
        """Column form rows serialize to the supported Table Schema vocabulary.

        This matters because the visual editor must write the exact descriptor
        the runtime parser already consumes, including composite-primary-key
        order and type-specific constraints.
        """
        data = {
            "name": "Check coordinates",
            "delimiter": ",",
            "has_header": "on",
            **_column_formset_data(
                [
                    {
                        "name": "site_id",
                        "type": "string",
                        "required": True,
                        "primary_key": True,
                        "pattern": r"^[A-Z]{2}-\d+$",
                    },
                    {
                        "name": "latitude",
                        "type": "number",
                        "required": True,
                        "minimum": "-90",
                        "maximum": "90",
                        "primary_key": True,
                    },
                    {
                        "name": "status",
                        "type": "string",
                        "enum_values": "present\nabsent",
                    },
                ],
            ),
        }

        form = TabularStepConfigForm(data=data)

        self.assertTrue(form.is_valid(), form.errors.as_json())
        descriptor = form.cleaned_data["descriptor"]
        self.assertEqual(descriptor["primaryKey"], ["site_id", "latitude"])
        self.assertEqual(
            descriptor["fields"][1]["constraints"],
            {"required": True, "minimum": -90.0, "maximum": 90.0},
        )
        self.assertEqual(
            descriptor["fields"][2]["constraints"]["enum"],
            ["present", "absent"],
        )
        self.assertEqual(form.cleaned_data["schema_source"], "editor")

    def test_column_editor_preserves_unexposed_descriptor_metadata(self):
        """Editing a supported constraint must not erase richer imported data.

        Table Schema descriptors can carry titles and extension keys that this
        V1 editor does not expose. Preserving those keys makes the editor a
        respectful round-trip rather than a lossy format converter.
        """
        base_descriptor = {
            "title": "Survey export",
            "x-project": "coastal",
            "fields": [
                {
                    "name": "latitude",
                    "title": "Latitude",
                    "type": "number",
                    "constraints": {"minimum": -90, "x-quality": "measured"},
                },
            ],
        }
        data = {
            "name": "Check coordinates",
            **_column_formset_data(
                [
                    {
                        "original_name": "latitude",
                        "name": "latitude",
                        "type": "number",
                        "minimum": "-80",
                    },
                ],
                base_descriptor=base_descriptor,
            ),
        }

        form = TabularStepConfigForm(data=data)

        self.assertTrue(form.is_valid(), form.errors.as_json())
        descriptor = form.cleaned_data["descriptor"]
        self.assertEqual(descriptor["title"], "Survey export")
        self.assertEqual(descriptor["x-project"], "coastal")
        self.assertEqual(descriptor["fields"][0]["title"], "Latitude")
        self.assertEqual(
            descriptor["fields"][0]["constraints"],
            {"minimum": -80.0, "x-quality": "measured"},
        )

    def test_column_editor_serializes_conditional_requiredness(self):
        """The V2 no-CEL widget writes the narrow Validibot schema extension.

        The target remains optional by default, but native validation requires
        it whenever the selected companion column appears in a submitted file.
        """
        form = TabularStepConfigForm(
            data={
                "name": "Conditional extension columns",
                **_column_formset_data(
                    [
                        {"name": "measurementType", "type": "string"},
                        {
                            "name": "measurementTypeID",
                            "type": "string",
                            "required_when_present": "measurementType",
                        },
                    ],
                ),
            },
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        constraints = form.cleaned_data["descriptor"]["fields"][1]["constraints"]
        self.assertEqual(
            constraints["x-validibot-requiredWhenPresent"],
            "measurementType",
        )

    def test_column_editor_rejects_unknown_conditional_trigger(self):
        """A stale or forged companion-column value cannot enter the schema."""
        form = TabularStepConfigForm(
            data={
                "name": "Bad conditional",
                **_column_formset_data(
                    [
                        {
                            "name": "measurementTypeID",
                            "required_when_present": "missing",
                        },
                    ],
                ),
            },
        )

        self.assertFalse(form.is_valid())
        self.assertIn("declared in this schema", str(form.column_formset.errors))

    def test_column_editor_rejects_case_colliding_names(self):
        """Case-only name collisions are rejected in the editor.

        Runtime column addressing is case-sensitive while Table Schema name
        uniqueness is not, so allowing ``Lat`` and ``lat`` would create an
        ambiguous schema that cannot be addressed safely in row assertions.
        """
        data = {
            "name": "Bad columns",
            **_column_formset_data(
                [
                    {"name": "Lat", "type": "number"},
                    {"name": "lat", "type": "number"},
                ],
            ),
        }

        form = TabularStepConfigForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertIn("letter case", str(form.column_formset.non_form_errors()))

    def test_column_editor_rejects_constraints_for_wrong_type(self):
        """Numeric limits on a text column are field errors, not silent no-ops.

        Immediate authoring feedback avoids a schema that appears constrained in
        the UI while the runtime correctly ignores an inapplicable constraint.
        """
        data = {
            "name": "Bad shape",
            **_column_formset_data(
                [{"name": "status", "type": "string", "minimum": "1"}],
            ),
        }

        form = TabularStepConfigForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertIn("minimum", form.column_formset.forms[0].errors)

    def test_column_editor_serializes_the_explicit_formset_order(self):
        """Move controls must change persisted field order, not only the DOM.

        Headerless files align values by position, so the hidden ``ORDER``
        values are part of the validation contract. This proves a browser move
        survives the POST and also controls composite-primary-key ordering.
        """
        data = {
            "name": "Ordered columns",
            **_column_formset_data(
                [
                    {
                        "name": "first",
                        "type": "string",
                        "primary_key": True,
                        "order": 2,
                    },
                    {
                        "name": "second",
                        "type": "string",
                        "primary_key": True,
                        "order": 1,
                    },
                ],
            ),
        }

        form = TabularStepConfigForm(data=data)

        self.assertTrue(form.is_valid(), form.errors.as_json())
        descriptor = form.cleaned_data["descriptor"]
        self.assertEqual(
            [field["name"] for field in descriptor["fields"]],
            ["second", "first"],
        )
        self.assertEqual(descriptor["primaryKey"], ["second", "first"])

    def test_uploaded_descriptor_is_validated_and_reports_compatibility(self):
        """JSON descriptor upload is equivalent to paste but remains honest.

        Unsupported Table Schema features are preserved for round-tripping,
        while the cleaned form exposes warnings so the redirect can tell the
        author exactly what V1 will not enforce.
        """
        descriptor = {
            "foreignKeys": [{"fields": "id", "reference": {"resource": "x"}}],
            "fields": [
                {
                    "name": "id",
                    "type": "geopoint",
                    "decimalChar": ",",
                },
            ],
        }
        uploaded = SimpleUploadedFile(
            "schema.json",
            json.dumps(descriptor).encode(),
            content_type="application/json",
        )
        form = TabularStepConfigForm(
            data={"name": "Imported schema"},
            files={"schema_file": uploaded},
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        self.assertEqual(form.cleaned_data["schema_source"], "upload")
        warnings = " ".join(form.cleaned_data["schema_warnings"])
        self.assertIn("Foreign keys", warnings)
        self.assertIn("Unsupported field types", warnings)
        self.assertIn("Locale-specific", warnings)


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
        self.assertContains(response, "Define expected columns")
        # The schema source tools are now header-launched modals rather than
        # inline cards, so assert their launch buttons are present.
        self.assertContains(response, "Infer from Sample")
        self.assertContains(response, "Import Table Schema")
        self.assertContains(response, "data-tabular-column-editor")
        self.assertNotContains(response, 'id="tabular-assertions-heading"')
        self.assertContains(response, "Required when another column exists")
        self.assertContains(response, f"#workflow-step-{step.pk}")
        # Every column loads collapsed: the details accordion is present with a
        # chevron toggle, and no column is auto-expanded (no "show" class) on a
        # clean GET. A failed submit is the only case that opens one.
        self.assertContains(response, "data-tabular-details-toggle")
        self.assertNotContains(response, "tabular-column-card__details show")

    def test_settings_page_uses_only_the_shared_breadcrumb(self):
        """Tabular settings should extend the top trail without duplicating it.

        The shared application breadcrumb preserves workflow and step context,
        so rendering a second trail inside the page header adds redundant
        navigation and gives the current page two competing labels.
        """
        workflow, step = self._tabular_workflow_and_step()
        _login_as_author(self.client, workflow)

        response = self.client.get(self._settings_url(workflow, step))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [str(crumb["name"]) for crumb in response.context["breadcrumbs"]],
            [
                "Workflows",
                workflow.name,
                step.step_number_display,
                "Tabular settings",
            ],
        )
        html = response.content.decode()
        self.assertEqual(html.count('<ol class="breadcrumb'), 1)

    def test_settings_page_uses_full_width_header_and_step_back_link(self):
        """The specialized editor should match the step page navigation pattern.

        Authors need an obvious route back to the workflow step, while the
        full-screen editor uses the whole width (no left sidebar) and renders
        the reusable editor-card shell instead of the generic form card.
        """
        workflow, step = self._tabular_workflow_and_step()
        _login_as_author(self.client, workflow)

        response = self.client.get(self._settings_url(workflow, step))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        step_url = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        self.assertContains(response, f'href="{step_url}"')
        self.assertContains(response, "bi-chevron-double-left")
        self.assertContains(
            response,
            '<div class="btn btn-primary bg-blue-lt border-0">',
        )
        subject_icon = response.context["subject_details"]["icon"]
        self.assertNotContains(response, f"bi {subject_icon} text-primary fs-4")
        # The viewport-locked editor keeps the standard collapsible app
        # navigation and uses the reusable sticky-footer editor card.
        self.assertContains(response, "app-viewport-locked")
        self.assertContains(response, 'id="tabular-step-settings"')
        self.assertContains(response, 'id="app-left-nav"')
        self.assertContains(response, 'id="app-left-nav-toggle"')
        self.assertIn("editor-shell", html)
        self.assertIn('class="card app-card editor-card"', html)
        self.assertIn(
            'class="card-footer d-flex flex-wrap justify-content-end '
            'align-items-center gap-2"',
            html,
        )
        self.assertContains(response, '<a class="btn btn-light"')
        self.assertNotContains(response, "bi-x-lg")
        self.assertNotContains(response, '<span class="badge text-bg-primary">')

    def test_create_page_ends_the_breadcrumb_with_tabular_settings(self):
        """New Tabular steps should use the editor's real page label.

        The create route renders the same specialized settings surface, so its
        breadcrumb should not fall back to the generic "Add step" label.
        """
        workflow, step = self._tabular_workflow_and_step()
        _login_as_author(self.client, workflow)
        create_url = reverse(
            "workflows:workflow_step_create",
            kwargs={"pk": workflow.pk, "validator_id": step.validator_id},
        )

        response = self.client.get(create_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [str(crumb["name"]) for crumb in response.context["breadcrumbs"]],
            ["Workflows", workflow.name, "Tabular settings"],
        )

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

    def test_large_schema_survives_a_dialect_only_edit(self):
        """A schema larger than the 1200-char preview is not corrupted when the
        author re-saves after changing only the delimiter.

        This is the P2 regression. The edit textarea used to be pre-filled with
        the 1200-char *preview*; a normal browser re-POST then sent that
        truncated JSON back as a replacement, either invalidating the schema or
        overwriting it with partial content. The fix starts the textarea empty
        (so an unchanged schema takes the "keep" path) and shows the full schema
        read-only. Here we save a >1200-char schema, then POST with an empty
        table_schema and a new delimiter, and assert the stored descriptor is
        byte-for-byte intact while the delimiter updates.
        """
        workflow, step = self._tabular_workflow_and_step()
        _login_as_author(self.client, workflow)

        # A descriptor comfortably larger than the 1200-char preview cap, with
        # unique field names (duplicates are rejected — see the P1 fix).
        big_descriptor = json.dumps(
            {
                "fields": [
                    {"name": f"column_{i:03d}", "type": "string"} for i in range(60)
                ],
            },
        )
        self.assertGreater(len(big_descriptor), 1200)

        # First save establishes the large schema.
        first = self.client.post(
            self._settings_url(workflow, step),
            data={
                "name": "Big schema step",
                "table_schema": big_descriptor,
                "delimiter": ",",
                "has_header": "on",
            },
        )
        self.assertEqual(first.status_code, 302)
        step.refresh_from_db()
        self.assertEqual(step.ruleset.rules_text, big_descriptor)

        # The settings page shows the *full* schema read-only — proving the edit
        # view no longer truncates it. A late field name only present past the
        # 1200-char mark must appear in the rendered page.
        page = self.client.get(self._settings_url(workflow, step))
        self.assertContains(page, "column_059")

        # Re-save changing ONLY the delimiter, with an empty schema textarea
        # (what a browser sends when the author doesn't touch the schema).
        second = self.client.post(
            self._settings_url(workflow, step),
            data={
                "name": "Big schema step",
                "table_schema": "",
                "delimiter": ";",
                "has_header": "on",
            },
        )
        self.assertEqual(second.status_code, 302)
        step.refresh_from_db()
        # Schema preserved byte-for-byte; only the delimiter changed.
        self.assertEqual(step.ruleset.rules_text, big_descriptor)
        self.assertEqual(step.ruleset.metadata["delimiter"], ";")
        self.assertEqual(step.typed_config.schema_source, "keep")

    def test_settings_post_saves_column_editor_descriptor(self):
        """The full settings request persists formset-authored columns.

        This is the primary UI path: the request must reach
        ``build_tabular_config`` with the same descriptor proven in the form
        unit tests, then update the summary metadata used by the step page.
        """
        workflow, step = self._tabular_workflow_and_step()
        _login_as_author(self.client, workflow)
        data = {
            "name": "Validate meter export",
            "delimiter": ",",
            "has_header": "on",
            **_column_formset_data(
                [
                    {
                        "name": "meter_id",
                        "type": "string",
                        "required": True,
                        "primary_key": True,
                    },
                    {
                        "name": "reading",
                        "type": "number",
                        "minimum": "0",
                    },
                ],
            ),
        }

        response = self.client.post(self._settings_url(workflow, step), data=data)

        self.assertEqual(response.status_code, 302)
        step.refresh_from_db()
        descriptor = json.loads(step.ruleset.rules_text)
        self.assertEqual(
            [field["name"] for field in descriptor["fields"]],
            ["meter_id", "reading"],
        )
        self.assertEqual(descriptor["primaryKey"], ["meter_id"])
        self.assertEqual(step.typed_config.column_count, 2)

    def test_add_column_htmx_endpoint_returns_next_prefixed_row(self):
        """HTMx row insertion advances the management count and field prefix.

        Formset prefixes are the persistence contract. A visually added row
        with a duplicate index would overwrite another column on POST, so the
        endpoint response is tested at the HTML boundary.
        """
        workflow, step = self._tabular_workflow_and_step()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_tabular_columns_existing",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )

        response = self.client.get(
            url, {"columns-TOTAL_FORMS": "2"}, headers={"hx-request": "true"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="columns-2-name"')
        self.assertContains(response, 'name="columns-2-DELETE"')
        self.assertContains(response, 'value="3"')
        self.assertContains(response, 'hx-swap-oob="outerHTML"')

    def test_import_endpoint_previews_then_applies_to_column_editor(self):
        """Import protects unsaved work with an explicit preview/apply step.

        The first response keeps current columns and presents the proposal.
        Applying that proposal then lands in the same editable row surface as
        manual authoring, so replacement is deliberate rather than accidental.
        """
        workflow, step = self._tabular_workflow_and_step()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_tabular_schema_import_existing",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )

        response = self.client.post(
            url,
            {"table_schema": _DESCRIPTOR, "schema_base": "{}"},
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review imported schema")
        self.assertContains(response, "Apply proposed schema")
        self.assertContains(response, 'name="pending_schema"')
        self.assertNotContains(response, 'value="lat"')

        apply_url = reverse(
            "workflows:workflow_tabular_schema_apply_existing",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        applied = self.client.post(
            apply_url,
            {
                "pending_schema": _DESCRIPTOR,
                "schema_base": "{}",
                **_column_formset_data(
                    [{"name": "current", "type": "string"}],
                ),
            },
            headers={"hx-request": "true"},
        )

        self.assertEqual(applied.status_code, 200)
        self.assertContains(applied, "Proposed columns applied")
        self.assertContains(applied, 'value="lat"')
        self.assertContains(applied, 'value="lon"')

    def test_invalid_import_keeps_current_columns_and_shows_error(self):
        """A failed import does not destroy unsaved column-editor work.

        HTMx posts the surrounding form; the error response binds those rows
        back into the replacement workspace so authors can fix the JSON without
        re-entering their existing columns.
        """
        workflow, step = self._tabular_workflow_and_step()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_tabular_schema_import_existing",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        data = {
            "table_schema": "{not json",
            **_column_formset_data([{"name": "unsaved_column", "type": "string"}]),
        }

        response = self.client.post(url, data, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Could not import the descriptor")
        self.assertContains(response, 'value="unsaved_column"')

    def test_infer_endpoint_previews_columns_and_resolves_dialect(self):
        """Inference previews typed rows and updates dialect controls.

        The sample upload cannot be replayed after the response, so inference
        stores the proposed descriptor in the preview. Applying that descriptor
        is a separate request and the resolved dialect is still returned
        through HTMx out-of-band swaps.
        """
        workflow, step = self._tabular_workflow_and_step()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_tabular_schema_infer_existing",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        sample = SimpleUploadedFile(
            "sample.csv",
            b"site_id,reading\nA-1,12.5\nA-2,13.75\n",
            content_type="text/csv",
        )

        response = self.client.post(
            url,
            {
                "sample_file": sample,
                "delimiter": "",
                "has_header": "on",
                "schema_base": "{}",
            },
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review inferred schema")
        self.assertContains(response, "Apply proposed schema")
        self.assertContains(response, "site_id")
        self.assertNotContains(response, 'value="site_id"')
        self.assertContains(response, 'hx-swap-oob="outerHTML"')

        apply_url = reverse(
            "workflows:workflow_tabular_schema_apply_existing",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        pending = json.dumps(
            {
                "fields": [
                    {"name": "site_id", "type": "string"},
                    {"name": "reading", "type": "number"},
                ],
            },
        )
        applied = self.client.post(
            apply_url,
            {
                "pending_schema": pending,
                "schema_base": "{}",
                **_column_formset_data(
                    [{"name": "current", "type": "string"}],
                ),
            },
            headers={"hx-request": "true"},
        )

        self.assertContains(applied, 'value="site_id"')
        self.assertContains(applied, 'value="reading"')
        self.assertContains(applied, 'value="number" selected')

    def test_import_preview_reports_unsupported_features(self):
        """Import compatibility warnings are visible before replacement.

        Preserving unsupported keys is not enough: authors must know that
        foreign keys and exotic types will not be enforced before they choose
        to apply the proposal.
        """
        workflow, step = self._tabular_workflow_and_step()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_tabular_schema_import_existing",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        descriptor = json.dumps(
            {
                "foreignKeys": [{"fields": "id", "reference": {"resource": "x"}}],
                "fields": [{"name": "id", "type": "geopoint"}],
            },
        )

        response = self.client.post(
            url,
            {"table_schema": descriptor, "schema_base": "{}"},
            headers={"hx-request": "true"},
        )

        self.assertContains(response, "Compatibility report")
        self.assertContains(response, "Foreign keys")
        self.assertContains(response, "Unsupported field types")

    def test_saved_descriptor_can_be_downloaded(self):
        """A saved schema is exportable as portable JSON.

        The editor adopts Table Schema partly to avoid lock-in, so authors need
        a direct way to retrieve the exact descriptor persisted on the ruleset.
        """
        workflow, step = self._tabular_workflow_and_step()
        step.ruleset = RulesetFactory(org=workflow.org)
        step.save(update_fields=["ruleset"])
        step.ruleset.rules_text = _DESCRIPTOR
        step.ruleset.save(update_fields=["rules_text"])
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_tabular_schema_export_existing",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), _DESCRIPTOR)
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        self.assertIn("attachment;", response["Content-Disposition"])


class TabularSchemaChangeRevalidationTests(TestCase):
    """A schema edit must not silently orphan a saved row/column assertion.

    Column references are validated when an *assertion* is saved, but the schema
    editor changes the columns out from under them. Renaming, deleting, or
    retyping a referenced column would turn a valid saved assertion into a
    run-time error — so the form re-checks the ruleset's assertions against the
    new schema and refuses the save with an actionable message.
    """

    def _step_with_lat_row_assertion(self):
        """A tabular step whose ruleset has a row assertion referencing ``lat``."""
        validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            supports_assertions=True,
        )
        ruleset = RulesetFactory(
            ruleset_type=RulesetType.TABULAR,
            rules_text=json.dumps({"fields": [{"name": "lat", "type": "number"}]}),
        )
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            rhs={"expr": "row.lat >= -90"},
            options={"tabular_stage": "row"},
            severity=Severity.ERROR,
        )
        return WorkflowStepFactory(validator=validator, ruleset=ruleset)

    def test_removing_a_referenced_column_blocks_save(self):
        """Renaming ``lat`` away while a row assertion references it fails save."""
        step = self._step_with_lat_row_assertion()
        form = TabularStepConfigForm(
            data={
                "name": "Check submission",
                # New schema drops ``lat`` (renamed to ``latitude``).
                "table_schema": json.dumps(
                    {"fields": [{"name": "latitude", "type": "number"}]},
                ),
                "encoding": "utf-8",
                "delimiter": "",
                "has_header": "on",
            },
            step=step,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("would break existing assertions", str(form.errors))

    def test_keeping_the_referenced_column_still_validates(self):
        """A schema change that preserves ``lat`` does not block the save."""
        step = self._step_with_lat_row_assertion()
        form = TabularStepConfigForm(
            data={
                "name": "Check submission",
                # ``lat`` is still present (a sibling column was added).
                "table_schema": json.dumps(
                    {
                        "fields": [
                            {"name": "lat", "type": "number"},
                            {"name": "lon", "type": "number"},
                        ],
                    },
                ),
                "encoding": "utf-8",
                "delimiter": "",
                "has_header": "on",
            },
            step=step,
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())
