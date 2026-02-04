from __future__ import annotations

import json
from html.parser import HTMLParser
from http import HTTPStatus

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import CertificationActionType
from validibot.actions.constants import IntegrationActionType
from validibot.actions.models import ActionDefinition
from validibot.actions.models import SignedCertificateAction
from validibot.actions.models import SlackMessageAction
from validibot.submissions.constants import SubmissionFileType
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorRuleType
from validibot.validations.models import Validator
from validibot.validations.tests.factories import CustomValidatorFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.models import WorkflowStep
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def seed_roles(db):
    ensure_all_roles_exist()


def ensure_validator(validation_type: str, slug: str, name: str) -> Validator:
    return Validator.objects.get_or_create(
        validation_type=validation_type,
        slug=slug,
        defaults={"name": name, "description": name},
    )[0]


def make_action_definition(
    *,
    category: str = ActionCategoryType.INTEGRATION,
    name: str | None = None,
    type_value: str | None = None,
) -> ActionDefinition:
    if type_value is None:
        type_value = (
            IntegrationActionType.SLACK_MESSAGE
            if category == ActionCategoryType.INTEGRATION
            else CertificationActionType.SIGNED_CERTIFICATE
        )
    slug = f"{category.lower()}-{type_value.lower()}"
    definition, _ = ActionDefinition.objects.get_or_create(
        action_category=category,
        type=type_value,
        defaults={
            "slug": slug,
            "name": name or f"{category.title()} action",
            "description": "Automated test action",
            "icon": "bi-gear",
        },
    )
    if name:
        definition.name = name
        definition.save(update_fields=["name"])
    return definition


def _login_for_workflow(client, workflow):
    user = workflow.user
    membership = user.memberships.get(org=workflow.org)
    membership.set_roles({RoleCode.AUTHOR})
    user.set_current_org(workflow.org)
    user.refresh_from_db()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()


def _login_user_for_org(client, user, org):
    user.set_current_org(org)
    user.refresh_from_db()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()


def _select_step_option(client, workflow, value: str) -> str:
    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.post(
        url,
        data={"stage": "select", "choice": value},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    redirect_url = response.headers.get("HX-Redirect")
    assert redirect_url, "wizard should instruct the client to navigate to the editor"
    return redirect_url


def _select_validator(client, workflow, validator) -> str:
    return _select_step_option(client, workflow, f"validator:{validator.pk}")


def _select_action(client, workflow, definition: ActionDefinition) -> str:
    return _select_step_option(client, workflow, f"action:{definition.pk}")


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


def test_wizard_redirects_to_action_create_view(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(name="Slack message")

    redirect_url = _select_action(client, workflow, definition)

    expected = reverse(
        "workflows:workflow_step_action_create",
        args=[workflow.pk, definition.pk],
    )
    assert expected in redirect_url


def test_wizard_lists_action_tabs(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    integration_def = make_action_definition(
        category=ActionCategoryType.INTEGRATION,
        name="Send Slack message",
    )
    certification_def = make_action_definition(
        category=ActionCategoryType.CERTIFICATION,
        name="Issue certificate",
    )

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(url, HTTP_HX_REQUEST="true")
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert integration_def.name in html
    assert certification_def.name in html
    assert "Integrations" in html
    assert "Certifications" in html


def test_wizard_shows_xml_validator_even_when_incompatible_file_type(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    xml_validator = ensure_validator(
        ValidationType.XML_SCHEMA,
        "xml-validator",
        "XML Schema",
    )

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(url, HTTP_HX_REQUEST="true")

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert xml_validator.name in html
    assert f'value="validator:{xml_validator.pk}"' in html
    assert f'value="validator:{xml_validator.pk}" disabled' in html


def test_fmi_validator_enabled_for_json_workflow(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    fmi_validator = ensure_validator(
        ValidationType.FMI,
        "fmi-validator",
        "FMI Validator",
    )

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(url, HTTP_HX_REQUEST="true")

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    value = f'value="validator:{fmi_validator.pk}"'
    assert value in html
    assert f"{value} disabled" not in html


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
    assert response.status_code == HTTPStatus.OK
    assert "Add workflow step" in response.content.decode()

    schema_text = json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"sku": {"type": "string"}},
        },
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
    assert response.status_code == HTTPStatus.FOUND

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
    assert (
        step.ruleset.metadata.get("schema_type")
        == JSONSchemaVersion.DRAFT_2020_12.value
    )


def test_create_view_with_custom_validator(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    custom_validator = CustomValidatorFactory(org=workflow.org)

    create_url = _select_validator(client, workflow, custom_validator.validator)
    response = client.post(
        create_url,
        data={
            "name": "Custom check",
            "description": "Runs custom logic",
            "notes": "Initial custom validator step",
        },
    )
    assert response.status_code == HTTPStatus.FOUND

    step = WorkflowStep.objects.get(workflow=workflow)
    assert step.validator == custom_validator.validator
    assert step.ruleset is not None
    assert step.ruleset.ruleset_type == custom_validator.validator.validation_type


def test_custom_validator_multiple_steps_get_unique_rulesets(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    custom_validator = CustomValidatorFactory(org=workflow.org)

    first_url = _select_validator(client, workflow, custom_validator.validator)
    second_url = _select_validator(client, workflow, custom_validator.validator)

    response = client.post(first_url, data={"name": "Step A"})
    assert response.status_code == HTTPStatus.FOUND

    response = client.post(second_url, data={"name": "Step B"})
    assert response.status_code == HTTPStatus.FOUND

    steps = WorkflowStep.objects.filter(workflow=workflow).order_by("order")
    assert steps.count() == 2  # noqa: PLR2004
    assert steps[0].ruleset != steps[1].ruleset


def test_create_view_creates_action_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(name="Notify team")

    create_url = _select_action(client, workflow, definition)

    response = client.get(create_url)
    assert response.status_code == HTTPStatus.OK
    assert "Add workflow step" in response.content.decode()

    response = client.post(
        create_url,
        data={
            "name": "Send Slack alert",
            "description": "Notify the #alerts channel after validation.",
            "notes": "Rotate webhook quarterly.",
            "message": "Validation finished successfully.",
        },
    )
    assert response.status_code == HTTPStatus.FOUND

    step = WorkflowStep.objects.get(workflow=workflow)
    assert step.validator is None
    assert step.action is not None
    assert step.action.definition == definition
    assert step.action.name == "Send Slack alert"
    variant = step.action.get_variant()
    assert isinstance(variant, SlackMessageAction)
    assert variant.message == "Validation finished successfully."
    assert step.description == "Notify the #alerts channel after validation."
    assert step.notes == "Rotate webhook quarterly."
    assert step.config.get("message") == "Validation finished successfully."


def test_create_certificate_action_uses_default_when_missing(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(
        category=ActionCategoryType.CERTIFICATION,
        name="Issue certificate",
    )

    create_url = _select_action(client, workflow, definition)

    response = client.post(
        create_url,
        data={
            "name": "Issue certificate",
            "description": "Provide certificates for passing runs.",
        },
    )
    assert response.status_code == HTTPStatus.FOUND

    step = WorkflowStep.objects.get(workflow=workflow)
    variant = step.action.get_variant()
    assert isinstance(variant, SignedCertificateAction)
    assert variant.certificate_template == "" or variant.certificate_template.name == ""
    assert variant.get_certificate_template_display_name().endswith(
        "default_signed_certificate.pdf",
    )
    assert step.config.get("certificate_template").endswith(
        "default_signed_certificate.pdf",
    )


def test_create_certificate_action_step(client, tmp_path):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(
        category=ActionCategoryType.CERTIFICATION,
        name="Issue certificate",
    )

    create_url = _select_action(client, workflow, definition)

    template_file = SimpleUploadedFile(
        "certificate.html",
        b"<html>Certificate</html>",
        content_type="text/html",
    )

    with override_settings(MEDIA_ROOT=str(tmp_path)):
        response = client.post(
            create_url,
            data={
                "name": "Issue certificate",
                "description": "Provide certificates for passing runs.",
                "certificate_template": template_file,
            },
        )
        assert response.status_code == HTTPStatus.FOUND

    step = WorkflowStep.objects.get(workflow=workflow)
    variant = step.action.get_variant()
    assert isinstance(variant, SignedCertificateAction)
    assert variant.certificate_template.name.endswith("certificate.html")
    assert step.config.get("certificate_template") == "certificate.html"


def test_update_certificate_action_step_allows_existing_template(client, tmp_path):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(
        category=ActionCategoryType.CERTIFICATION,
        name="Issue certificate",
    )

    original_template = SimpleUploadedFile(
        "original.html",
        b"<html>Original</html>",
        content_type="text/html",
    )

    with override_settings(MEDIA_ROOT=str(tmp_path)):
        action = SignedCertificateAction.objects.create(
            definition=definition,
            name="Issue certificate",
            description="Existing description",
            certificate_template=original_template,
        )
        step = WorkflowStep.objects.create(
            workflow=workflow,
            action=action,
            order=10,
            name="Issue certificate",
            description="Existing description",
            config={"certificate_template": "original.html"},
        )

        edit_url = reverse(
            "workflows:workflow_step_settings",
            args=[workflow.pk, step.pk],
        )
        response = client.post(
            edit_url,
            data={
                "name": "Issue certificate",
                "description": "Existing description",
                "notes": "",
            },
        )
        assert response.status_code == HTTPStatus.FOUND

        step.refresh_from_db()
        variant = step.action.get_variant()
        assert variant.certificate_template.name.endswith("original.html")


def test_create_view_validates_missing_upload(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.JSON_SCHEMA,
        "json-validator",
        "JSON Validator",
    )

    create_url = _select_validator(client, workflow, validator)

    response = client.post(
        create_url,
        data={
            "name": "JSON Schema",
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
        },
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    html = response.content.decode()
    assert "Add content directly or upload a file." in html


def test_create_json_schema_rejects_text_and_file(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.JSON_SCHEMA,
        "json-validator",
        "JSON Validator",
    )

    create_url = _select_validator(client, workflow, validator)
    fake_file = SimpleUploadedFile(
        "schema.json",
        b"{}",
        content_type="application/json",
    )
    response = client.post(
        create_url,
        data={
            "name": "JSON Schema",
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
            "schema_text": '{"type": "object"}',
            "schema_file": fake_file,
        },
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    body = response.content.decode()
    assert "Paste the schema or upload a file, not both." in body


def test_create_xml_step_requires_schema_text(client):
    workflow = WorkflowFactory(allowed_file_types=[SubmissionFileType.XML])
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.XML_SCHEMA,
        "xml-validator",
        "XML Validator",
    )

    create_url = _select_validator(client, workflow, validator)
    response = client.post(
        create_url,
        data={
            "name": "XML Schema",
            "schema_type": "XSD",
            "schema_text": "",
        },
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    html = response.content.decode()
    assert "We found a few issues" in html
    assert "Add content directly or upload a file." in html
    assert 'name="schema_text"' in html
    assert "is-invalid" in html
    assert '<button type="submit" class="btn btn-secondary">' in html
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
    assert response.status_code == HTTPStatus.BAD_REQUEST
    html = response.content.decode()
    assert "We found a few issues" in html
    assert "Add at least one policy rule." in html
    assert '<button type="submit" class="btn btn-secondary">' in html
    assert "Create step" in html


def test_create_energyplus_step_with_idf_checks(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.ENERGYPLUS, "energyplus", "EnergyPlus")

    create_url = _select_validator(client, workflow, validator)
    response = client.post(
        create_url,
        data={
            "name": "EnergyPlus QA",
            "weather_file": "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
            "run_simulation": "on",
            "idf_checks": ["duplicate-names", "hvac-sizing"],
        },
    )
    # 302 redirect to assertions page on success
    assert response.status_code == HTTPStatus.FOUND
    step = workflow.steps.first()
    assert step is not None
    assert step.config["idf_checks"] == ["duplicate-names", "hvac-sizing"]
    assert step.config["run_simulation"] is True


def test_step_settings_does_not_expose_validator_selector(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.JSON_SCHEMA,
        "json-validator",
        "JSON Validator",
    )
    step = WorkflowStepFactory(workflow=workflow, validator=validator)

    edit_url = reverse("workflows:workflow_step_settings", args=[workflow.pk, step.pk])
    response = client.get(edit_url)

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert 'name="validator_choice"' not in body


def test_create_basic_step_uses_minimal_fields(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(
        ValidationType.BASIC,
        "basic-validator",
        "Manual Assertions",
    )

    create_url = _select_validator(client, workflow, validator)
    response = client.post(
        create_url,
        data={
            "name": "Manual assertions",
            "description": "Hand-written checks",
            "notes": "Remember to add derivations later",
        },
    )

    assert response.status_code == HTTPStatus.FOUND
    step = WorkflowStep.objects.get(workflow=workflow, validator=validator)
    assert step.name == "Manual assertions"
    assert step.config == {}


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
                },
            ],
        },
    )

    edit_url = reverse("workflows:workflow_step_settings", args=[workflow.pk, step.pk])
    response = client.get(edit_url)
    assert response.status_code == HTTPStatus.OK
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
    assert response.status_code == HTTPStatus.FOUND
    step.refresh_from_db()
    assert step.name == "AI step updated"
    assert step.description == "Tweaked summary"
    assert step.notes == "Revised notes"
    assert step.config["template"] == "ai_critic"
    assert step.config["mode"] == "ADVISORY"


def test_update_view_prefills_action_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(name="Slack alert")
    action = SlackMessageAction.objects.create(
        definition=definition,
        name="Alert ops",
        description="Existing description",
        message="Original message",
    )
    step = WorkflowStep.objects.create(
        workflow=workflow,
        action=action,
        order=10,
        name="Alert ops",
        description="Existing description",
        notes="Existing notes",
        config={"message": "Original message"},
    )

    edit_url = reverse("workflows:workflow_step_settings", args=[workflow.pk, step.pk])
    response = client.get(edit_url)
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "Original message" in html
    assert "Existing notes" in html
    assert "Alert ops" in html

    response = client.post(
        edit_url,
        data={
            "name": "Alert ops updated",
            "description": "Updated description",
            "notes": "Updated notes",
            "message": "Escalate to ops channel.",
        },
    )
    assert response.status_code == HTTPStatus.FOUND
    step.refresh_from_db()
    action_variant = step.action.get_variant()
    assert step.action.name == "Alert ops updated"
    assert step.description == "Updated description"
    assert step.notes == "Updated notes"
    assert isinstance(action_variant, SlackMessageAction)
    assert action_variant.message == "Escalate to ops channel."


def test_step_form_navigation_links(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")
    first_step = WorkflowStepFactory(workflow=workflow, validator=validator, order=10)
    second_step = WorkflowStepFactory(workflow=workflow, validator=validator, order=20)

    edit_url = reverse(
        "workflows:workflow_step_settings",
        args=[workflow.pk, second_step.pk],
    )
    response = client.get(edit_url)
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert (
        reverse("workflows:workflow_step_edit", args=[workflow.pk, first_step.pk])
        in html
    )
    assert "Previous step" in html
    assert "Next step" not in html


def test_step_editor_shows_default_assertions_card(client):
    """Verify default assertions card shows when validator has rules defined."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ValidatorFactory(
        validation_type=ValidationType.CUSTOM_VALIDATOR,
        slug="custom-validator-with-defaults",
    )
    validator.rules.create(
        name="Baseline price check",
        rule_type=ValidatorRuleType.CEL_EXPRESSION,
        expression="payload.price > 0",
        order=0,
    )
    step = WorkflowStepFactory(workflow=workflow, validator=validator, order=10)

    edit_url = reverse("workflows:workflow_step_edit", args=[workflow.pk, step.pk])
    response = client.get(edit_url)

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "default-assertions-card" in html
    assert (
        "Default assertions run by the validator selected for this step: 1 assertion."
        in html
    )
    assert "View default assertions" in html


def test_step_editor_hides_default_assertions_when_none(client):
    """Verify default assertions card is hidden when validator has no rules."""
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    validator = ValidatorFactory(
        validation_type=ValidationType.CUSTOM_VALIDATOR,
        slug="custom-validator-no-defaults",
    )
    step = WorkflowStepFactory(workflow=workflow, validator=validator, order=10)

    edit_url = reverse("workflows:workflow_step_edit", args=[workflow.pk, step.pk])
    response = client.get(edit_url)

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "default-assertions-card" not in html


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
    assert response.status_code == HTTPStatus.NO_CONTENT
    step_a.refresh_from_db()
    step_b.refresh_from_db()
    assert step_b.order == 10  # noqa: PLR2004
    assert step_a.order == 20  # noqa: PLR2004

    delete_url = reverse(
        "workflows:workflow_step_delete",
        args=[workflow.pk, step_a.pk],
    )
    response = client.post(delete_url, HTTP_HX_REQUEST="true")
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert list(workflow.steps.all()) == [step_b]


def test_step_create_rejects_validator_from_other_org(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    foreign_org = OrganizationFactory()
    bad_validator = ValidatorFactory(org=foreign_org, is_system=False)

    url = reverse(
        "workflows:workflow_step_create",
        args=[workflow.pk, bad_validator.pk],
    )
    response = client.get(url)
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_step_delete_requires_manager_role(client):
    workflow = WorkflowFactory()
    step = WorkflowStepFactory(workflow=workflow)
    user = UserFactory()
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    _login_user_for_org(client, user, workflow.org)

    url = reverse(
        "workflows:workflow_step_delete",
        args=[workflow.pk, step.pk],
    )
    response = client.post(url)
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert WorkflowStep.objects.filter(pk=step.pk).exists()


def test_step_move_requires_manager_role(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow, order=10)
    target_step = WorkflowStepFactory(workflow=workflow, order=20)
    user = UserFactory()
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    _login_user_for_org(client, user, workflow.org)

    url = reverse(
        "workflows:workflow_step_move",
        args=[workflow.pk, target_step.pk],
    )
    response = client.post(url, data={"direction": "up"})
    assert response.status_code == HTTPStatus.FORBIDDEN
    orders = list(
        workflow.steps.order_by("order").values_list("order", flat=True),
    )
    assert orders == [10, 20]


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

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Author notes" in body
    assert "Private deployment checklist" in body


def test_step_list_renders_action_step(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    definition = make_action_definition(name="Slack integration")
    action = SlackMessageAction.objects.create(
        definition=definition,
        name="Notify Slack",
        description="Send a Slack notification",
        message="Ping #alerts when the workflow completes.",
    )
    WorkflowStep.objects.create(
        workflow=workflow,
        action=action,
        order=10,
        name="Notify Slack",
        description="Send a Slack notification",
        config={"message": "Ping #alerts when the workflow completes."},
    )

    response = client.get(
        reverse("workflows:workflow_step_list", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "Notify Slack" in html
    assert definition.get_action_category_display() in html
    assert "#alerts" in html


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

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Author notes" not in body
    assert "Only authors should see this" not in body


def test_wizard_select_highlights_selected_card(client):
    workflow = WorkflowFactory()
    _login_for_workflow(client, workflow)
    ensure_validator(ValidationType.JSON_SCHEMA, "json-validator", "JSON Validator")
    validator_b = ensure_validator(ValidationType.AI_ASSIST, "ai-assist", "AI Assist")

    url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
    response = client.get(
        url,
        {"selected": f"validator:{validator_b.pk}"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    parser = _CardParser()
    parser.feed(html)
    assert parser.selected_cards == 1
    assert parser.checked_values == [f"validator:{validator_b.pk}"]


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
            and attrs_dict.get("name") == "choice"
        ):
            if any(name == "checked" for name, _ in attrs):
                self.checked_values.append(str(attrs_dict.get("value")))
