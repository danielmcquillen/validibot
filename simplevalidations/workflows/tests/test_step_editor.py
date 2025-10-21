from __future__ import annotations

import json
from html.parser import HTMLParser

import pytest
from django.urls import reverse

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


def start_wizard(client, workflow, validator):
    select_url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.post(
        select_url,
        data={"stage": "select", "validator": validator.pk},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert "stage" in response.content.decode()
    return select_url


def test_wizard_creates_json_schema_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ValidatorFactory(validation_type=ValidationType.JSON_SCHEMA, slug="json-validator")

    select_url = start_wizard(client, workflow, validator)

    schema_text = "{\"type\": \"object\"}"
    response = client.post(
        select_url,
        data={
            "stage": "configure",
            "validator_id": validator.pk,
            "name": "JSON Schema",
            "description": "Ensures posted documents follow the schema.",
            "notes": "Remember to update schema when payload changes.",
            "display_schema": "on",
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
            "schema_source": "text",
            "schema_text": schema_text,
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 204
    payload = json.loads(response.headers["HX-Trigger"])
    assert payload.get("steps-changed") is True

    step = WorkflowStep.objects.get(workflow=workflow)
    assert step.validator == validator
    assert step.ruleset is not None
    stored_schema = step.ruleset.rules
    assert "type" in stored_schema
    assert step.config["schema_source"] == "text"
    assert step.config["schema_type"] == JSONSchemaVersion.DRAFT_2020_12.value
    assert step.description == "Ensures posted documents follow the schema."
    assert step.notes == "Remember to update schema when payload changes."
    assert step.display_schema is True
    assert step.ruleset.metadata.get("schema_type") == JSONSchemaVersion.DRAFT_2020_12.value


def test_wizard_creates_ai_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")

    select_url = start_wizard(client, workflow, validator)

    response = client.post(
        select_url,
        data={
            "stage": "configure",
            "validator_id": validator.pk,
            "name": "Cooling policy",
            "description": "Checks cooling setpoints before publishing.",
            "notes": "Coordinate with HVAC team before changing limits.",
            "display_schema": "on",
            "template": "policy_check",
            "selectors": "$.zones[*].cooling_setpoint",
            "policy_rules": "$.zones[*].cooling_setpoint >= 18 | Cooling must be ≥18°C",
            "cost_cap_cents": 12,
            "mode": "BLOCKING",
        },
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 204

    step = WorkflowStep.objects.get(workflow=workflow)
    assert step.validator == validator
    assert step.config["template"] == "policy_check"
    assert step.config["mode"] == "BLOCKING"
    assert step.config["policy_rules"]
    assert step.description == "Checks cooling setpoints before publishing."
    assert step.notes == "Coordinate with HVAC team before changing limits."
    assert step.display_schema is False


def test_wizard_creates_energyplus_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS, slug="energyplus")

    select_url = start_wizard(client, workflow, validator)

    response = client.post(
        select_url,
        data={
            "stage": "configure",
            "validator_id": validator.pk,
            "name": "EnergyPlus QA",
            "description": "Runs EnergyPlus simulation checks.",
            "notes": "Keep aligned with mechanical baseline files.",
            "run_simulation": "on",
            "idf_checks": ["duplicate-names", "hvac-sizing"],
            "simulation_checks": ["eui-range"],
            "eui_min": "40",
            "eui_max": "80",
            "energyplus_notes": "Baseline office model",
        },
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 204

    step = WorkflowStep.objects.get(workflow=workflow)
    assert step.validator == validator
    assert step.config["run_simulation"] is True
    assert step.config["eui_band"]["min"] == 40.0
    assert step.config["eui_band"]["max"] == 80.0
    assert step.config["notes"] == "Baseline office model"
    assert step.description == "Runs EnergyPlus simulation checks."
    assert step.notes == "Keep aligned with mechanical baseline files."
    assert step.display_schema is False


def test_step_limit_blocks_additional_steps(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")

    for index in range(5):
        WorkflowStep.objects.create(
            workflow=workflow,
            validator=validator,
            order=(index + 1) * 10,
            name=f"Step {index}",
            config={"template": "ai_critic", "mode": "ADVISORY", "cost_cap_cents": 10},
        )

    wizard_url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(wizard_url, HTTP_HX_REQUEST="true")
    assert response.status_code == 409


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


def test_edit_ai_step_prefills_form(client):
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
                    "id": "rule-test",
                }
            ],
        },
    )

    wizard_url = reverse("workflows:workflow_step_wizard_existing", args=[workflow.pk, step.pk])
    response = client.get(wizard_url, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    content = response.content.decode()
    assert "policy_check" in content
    assert "Cooling must be" in content
    assert "Existing summary" in content
    assert "Existing author notes" in content


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


def test_json_schema_wizard_missing_upload_shows_error(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.JSON_SCHEMA, "json-validator", "JSON Validator")

    # Stage 1: move to configuration form
    select_url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.post(
        select_url,
        data={"stage": "select", "validator": validator.pk},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200

    # Stage 2: choose upload without providing file
    response = client.post(
        select_url,
        data={
            "stage": "configure",
            "validator_id": validator.pk,
            "name": "JSON Schema",
            "schema_source": "upload",
            "schema_text": "{\"type\":\"object\"}",
        },
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert "Upload a JSON schema file" in html
    assert "is-invalid" in html or "invalid-feedback" in html


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
        if tag == "input" and attrs_dict.get("type") == "radio" and attrs_dict.get("name") == "validator":
            if any(name == "checked" for name, _ in attrs):
                self.checked_values.append(str(attrs_dict.get("value")))
