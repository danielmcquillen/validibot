from __future__ import annotations

from html.parser import HTMLParser
import json

import pytest
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.constants import JSONSchemaVersion
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.models import Validator
from simplevalidations.validations.tests.factories import ValidatorFactory
from simplevalidations.workflows.models import WorkflowStep
from simplevalidations.workflows.tests.factories import WorkflowFactory
from simplevalidations.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def ensure_validator(validation_type: str, slug: str, name: str) -> Validator:
    return Validator.objects.get_or_create(
        validation_type=validation_type,
        slug=slug,
        defaults={"name": name, "description": name},
    )[0]


def _login_for_workflow(client, workflow):
    user = workflow.user
    user.set_current_org(workflow.org)
    user.refresh_from_db()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()


def _select_validator(client, workflow, validator) -> str:
    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.post(
        url,
        data={"stage": "select", "validator": validator.pk},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 204
    redirect_url = response.headers.get("HX-Redirect")
    assert redirect_url, "wizard should instruct the client to navigate to the editor"
    return redirect_url


def test_wizard_redirects_to_create_view(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")

    redirect_url = _select_validator(client, workflow, validator)

    expected = reverse(
        "workflows:workflow_step_create",
        args=[workflow.pk, validator.pk],
    )
    assert expected in redirect_url


def test_create_view_creates_json_schema_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        slug="json-validator",
    )

    create_url = _select_validator(client, workflow, validator)

    # GET renders the full-page editor
    response = client.get(create_url)
    assert response.status_code == 200
    assert "Add workflow step" in response.content.decode()

    schema_text = json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "sku": {"type": "string"}
            },
        }
    )
    response = client.post(
        create_url,
        data={
            "name": "JSON Schema",
            "description": "Ensures posted documents follow the schema.",
            "notes": "Remember to update schema when payload changes.",
            "display_schema": "on",
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
            "schema_text": schema_text,
        },
    )
    assert response.status_code == 302

    step = WorkflowStep.objects.get(workflow=workflow)
    assert step.validator == validator
    assert step.ruleset is not None
    stored_schema = step.ruleset.rules
    assert "$schema" in stored_schema
    assert "type" in stored_schema
    assert step.config["schema_source"] == "text"
    assert step.config["schema_type"] == JSONSchemaVersion.DRAFT_2020_12.value
    assert step.description == "Ensures posted documents follow the schema."
    assert step.notes == "Remember to update schema when payload changes."
    assert step.display_schema is True
    assert step.ruleset.metadata.get("schema_type") == JSONSchemaVersion.DRAFT_2020_12.value


def test_create_view_validates_missing_upload(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.JSON_SCHEMA, "json-validator", "JSON Validator")

    create_url = _select_validator(client, workflow, validator)

    response = client.post(
        create_url,
        data={
            "name": "JSON Schema",
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
        },
    )
    assert response.status_code == 400
    html = response.content.decode()
    assert "Add content directly or upload a file." in html


def test_create_json_schema_rejects_text_and_file(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.JSON_SCHEMA, "json-validator", "JSON Validator")

    create_url = _select_validator(client, workflow, validator)
    fake_file = SimpleUploadedFile("schema.json", b"{}", content_type="application/json")
    response = client.post(
        create_url,
        data={
            "name": "JSON Schema",
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
            "schema_text": "{\"type\": \"object\"}",
            "schema_file": fake_file,
        },
    )
    assert response.status_code == 400
    body = response.content.decode()
    assert "Paste the schema or upload a file, not both." in body


def test_create_xml_step_requires_schema_text(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.XML_SCHEMA, "xml-validator", "XML Validator")

    create_url = _select_validator(client, workflow, validator)
    response = client.post(
        create_url,
        data={
            "name": "XML Schema",
            "schema_type": "XSD",
            "schema_text": "",
        },
    )
    assert response.status_code == 400
    html = response.content.decode()
    assert "We found a few issues" in html
    assert "Add content directly or upload a file." in html
    assert 'name="schema_text"' in html
    assert "is-invalid" in html
    assert '<button type="submit" class="btn btn-primary">' in html
    assert "Create step" in html


def test_create_ai_policy_requires_rules(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")

    create_url = _select_validator(client, workflow, validator)
    response = client.post(
        create_url,
        data={
            "name": "Policy check",
            "template": "policy_check",
            "policy_rules": "",
            "selectors": "",
            "mode": "BLOCKING",
            "cost_cap_cents": 15,
        },
    )
    assert response.status_code == 400
    html = response.content.decode()
    assert "We found a few issues" in html
    assert "Add at least one policy rule." in html
    assert '<button type="submit" class="btn btn-primary">' in html
    assert "Create step" in html


def test_create_energyplus_requires_simulation_toggle_for_checks(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.ENERGYPLUS, "energyplus", "EnergyPlus")

    create_url = _select_validator(client, workflow, validator)
    response = client.post(
        create_url,
        data={
            "name": "EnergyPlus QA",
            "run_simulation": "",
            "idf_checks": ["duplicate-names"],
            "simulation_checks": ["eui-range"],
            "eui_min": "",
            "eui_max": "",
            "energyplus_notes": "",
        },
    )
    assert response.status_code == 400
    html = response.content.decode()
    assert "Run EnergyPlus simulation" in html
    assert "simulation_checks" in html
    assert '<button type="submit" class="btn btn-primary">' in html
    assert "Create step" in html


def test_update_view_prefills_ai_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")
    step = WorkflowStep.objects.create(
        workflow=workflow,
        validator=validator,
        order=10,
        name="AI step",
        description="Existing summary",
        notes="Existing author notes",
        config={
            "template": "policy_check",
            "mode": "BLOCKING",
            "cost_cap_cents": 20,
            "selectors": ["$.zones[*].cooling_setpoint"],
            "policy_rules": [
                {
                    "path": "$.zones[*].cooling_setpoint",
                    "operator": ">=",
                    "value": 18,
                    "value_b": None,
                    "message": "Cooling must be ≥18°C",
                }
            ],
        },
    )

    edit_url = reverse("workflows:workflow_step_edit", args=[workflow.pk, step.pk])
    response = client.get(edit_url)
    assert response.status_code == 200
    body = response.content.decode()
    assert "policy_check" in body
    assert "Cooling must be" in body
    assert "Existing summary" in body
    assert "Existing author notes" in body

    response = client.post(
        edit_url,
        data={
            "name": "AI step updated",
            "description": "Tweaked summary",
            "notes": "Revised notes",
            "template": "ai_critic",
            "selectors": "",
            "policy_rules": "",
            "cost_cap_cents": 15,
            "mode": "ADVISORY",
        },
    )
    assert response.status_code == 302
    step.refresh_from_db()
    assert step.name == "AI step updated"
    assert step.description == "Tweaked summary"
    assert step.notes == "Revised notes"
    assert step.config["template"] == "ai_critic"
    assert step.config["mode"] == "ADVISORY"


def test_step_form_navigation_links(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")
    first_step = WorkflowStepFactory(workflow=workflow, validator=validator, order=10)
    second_step = WorkflowStepFactory(workflow=workflow, validator=validator, order=20)

    edit_url = reverse("workflows:workflow_step_edit", args=[workflow.pk, second_step.pk])
    response = client.get(edit_url)
    assert response.status_code == 200
    html = response.content.decode()
    assert reverse("workflows:workflow_step_edit", args=[workflow.pk, first_step.pk]) in html
    assert "Previous step" in html
    assert "Next step" not in html


def test_move_and_delete_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")

    step_a = WorkflowStep.objects.create(
        workflow=workflow,
        validator=validator,
        order=10,
        name="First",
        config={"template": "ai_critic", "mode": "ADVISORY", "cost_cap_cents": 10},
    )
    step_b = WorkflowStep.objects.create(
        workflow=workflow,
        validator=validator,
        order=20,
        name="Second",
        config={"template": "ai_critic", "mode": "ADVISORY", "cost_cap_cents": 10},
    )

    move_url = reverse("workflows:workflow_step_move", args=[workflow.pk, step_b.pk])
    response = client.post(move_url, data={"direction": "up"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 204
    step_a.refresh_from_db()
    step_b.refresh_from_db()
    assert step_b.order == 10
    assert step_a.order == 20

    delete_url = reverse("workflows:workflow_step_delete", args=[workflow.pk, step_a.pk])
    response = client.post(delete_url, HTTP_HX_REQUEST="true")
    assert response.status_code == 204
    assert list(workflow.steps.all()) == [step_b]


def test_step_list_shows_author_notes_for_authors(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    WorkflowStepFactory(
        workflow=workflow,
        notes="Private deployment checklist",
        description="Performs strict validation",
    )

    response = client.get(
        reverse("workflows:workflow_step_list", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "Author notes" in body
    assert "Private deployment checklist" in body


def test_step_list_hides_author_notes_for_non_authors(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(
        workflow=workflow,
        notes="Only authors should see this",
        description="General description",
    )

    other_user = UserFactory()
    grant_role(other_user, workflow.org, RoleCode.EXECUTOR)
    other_user.set_current_org(workflow.org)
    client.force_login(other_user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()

    response = client.get(
        reverse("workflows:workflow_step_list", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "Author notes" not in body
    assert "Only authors should see this" not in body


def test_wizard_select_highlights_selected_card(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator_a = ensure_validator(ValidationType.JSON_SCHEMA, "json-validator", "JSON Validator")
    validator_b = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(url, {"selected": validator_b.pk}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    html = response.content.decode()
    parser = _CardParser()
    parser.feed(html)
    assert parser.selected_cards == 1
    assert parser.checked_values == [str(validator_b.pk)]


class _CardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.selected_cards = 0
        self.checked_values: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "label":
            classes = attrs_dict.get("class", "")
            if "validator-card" in classes and "is-selected" in classes:
                self.selected_cards += 1
        if (
            tag == "input"
            and attrs_dict.get("type") == "radio"
            and attrs_dict.get("name") == "validator"
        ):
            if any(name == "checked" for name, _ in attrs):
                self.checked_values.append(str(attrs_dict.get("value")))
