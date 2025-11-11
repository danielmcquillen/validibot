from __future__ import annotations

import html
import json
from http import HTTPStatus

import pytest
from django.urls import reverse
from rest_framework.response import Response

from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.constants import JSONSchemaVersion
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.constants import XMLSchemaType
from simplevalidations.validations.models import Ruleset
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.tests.factories import ValidatorFactory
from simplevalidations.workflows.tests.factories import WorkflowFactory
from simplevalidations.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def _force_login_for_workflow(client, workflow):
    user = workflow.user
    user.set_current_org(workflow.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()
    return user


def test_launch_page_requires_authentication(client):
    workflow = WorkflowFactory()
    url = reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk})

    response = client.get(url)

    assert response.status_code == HTTPStatus.FOUND
    assert "login" in response.url


def test_launch_page_renders_for_org_member(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    response = client.get(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk})
    )

    assert response.status_code == HTTPStatus.OK
    assert "Start Validation" in response.content.decode()


def test_launch_page_disables_form_without_steps(client):
    workflow = WorkflowFactory()
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    response = client.get(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk})
    )

    body = response.content.decode()
    assert response.status_code == HTTPStatus.OK
    assert "This workflow has no steps yet." in body
    assert "Start Validation" not in body


def test_launch_start_creates_run_and_returns_partial(client, monkeypatch):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    def fake_launch(self, request, org, workflow, submission, user_id, metadata):  # noqa: ANN001
        run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=workflow.project,
            user=request.user,
            status=ValidationRunStatus.PENDING,
        )
        return Response(
            {"id": str(run.pk), "status": ValidationRunStatus.PENDING},
            status=202,
        )

    monkeypatch.setattr(
        "simplevalidations.workflows.views.ValidationRunService.launch",
        fake_launch,
    )

    response = client.post(
        reverse("workflows:workflow_launch_start", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "payload": "{}",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    body = response.content.decode()
    assert "Run in progress" in body
    assert ValidationRun.objects.filter(workflow=workflow).count() == 1
    hx_trigger = response.headers.get("HX-Trigger")
    assert hx_trigger and "Validation run started" in hx_trigger


def test_launch_start_requires_executor_role(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    _force_login_for_workflow(client, workflow)

    response = client.post(
        reverse("workflows:workflow_launch_start", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "payload": "{}",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert (
        "You do not have permission to run this workflow" in response.content.decode()
    )


def test_public_info_view_accessible_when_enabled(client):
    workflow = WorkflowFactory(make_info_public=True)
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        slug="public-json",
    )
    schema_text = json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"sku": {"type": "string"}},
        }
    )
    ruleset = Ruleset.objects.create(
        org=workflow.org,
        user=workflow.user,
        ruleset_type=RulesetType.JSON_SCHEMA,
        name="Public schema",
    )
    ruleset.metadata = {
        "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
    }
    ruleset.rules_text = schema_text
    ruleset.save(update_fields=["metadata", "rules_text"])
    WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        description="Validates base product payload.",
        display_schema=True,
        ruleset=ruleset,
        config={
            "schema_source": "text",
            "schema_text_preview": schema_text[:100],
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
            "schema_type_label": str(JSONSchemaVersion.DRAFT_2020_12.label),
        },
    )

    response = client.get(
        reverse("workflow_public_info", kwargs={"workflow_uuid": workflow.uuid}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert workflow.name in body
    assert "All Workflows" in body
    assert html.escape(f"Workflow '{workflow.name}'") in body

    # Validation we can find the id "workflow-public-view" of the div that holds info
    assert 'id="workflow-public-view"' in body


def test_public_info_form_updates_visibility(client):
    workflow = WorkflowFactory(make_info_public=False)
    WorkflowStepFactory(workflow=workflow)
    _force_login_for_workflow(client, workflow)

    response = client.post(
        reverse("workflows:workflow_public_info_edit", kwargs={"pk": workflow.pk}),
        data={
            "title": "Public doc",
            "content_md": "## Overview\nDetails here.",
            "make_info_public": "on",
        },
    )

    assert response.status_code == HTTPStatus.FOUND
    workflow.refresh_from_db()
    assert workflow.make_info_public is True


def test_public_visibility_toggle_updates_card(client):
    workflow = WorkflowFactory(make_info_public=False)
    WorkflowStepFactory(workflow=workflow)
    _force_login_for_workflow(client, workflow)

    response = client.post(
        reverse("workflows:workflow_public_visibility", kwargs={"pk": workflow.pk}),
        data={"make_info_public": "true"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    workflow.refresh_from_db()
    assert workflow.make_info_public is True
    assert "Visible" in response.content.decode()


def test_launch_start_rejects_incompatible_file_type(client):
    workflow = WorkflowFactory(
        allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.XML],
    )
    WorkflowStepFactory(workflow=workflow)
    user = workflow.user
    user.set_current_org(workflow.org)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    client.force_login(user)

    response = client.post(
        reverse("workflows:workflow_launch_start", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.XML,
            "payload": "<data/>",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    body = response.content.decode()
    assert "does not support" in body


def test_public_info_view_hides_schema_when_not_shared(client):
    workflow = WorkflowFactory(make_info_public=True)
    validator = ValidatorFactory(
        validation_type=ValidationType.XML_SCHEMA,
        slug="public-xml",
    )
    xml_schema = """<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>\n  <xs:element name='item' type='xs:string'/>\n</xs:schema>"""
    ruleset = Ruleset.objects.create(
        org=workflow.org,
        user=workflow.user,
        ruleset_type=RulesetType.XML_SCHEMA,
        name="Private schema",
    )
    ruleset.metadata = {
        "schema_type": XMLSchemaType.XSD.value,
    }
    ruleset.rules_text = xml_schema
    ruleset.save(update_fields=["metadata", "rules_text"])
    WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        description="Validates XML payload.",
        display_schema=False,
        ruleset=ruleset,
        config={
            "schema_source": "text",
            "schema_text_preview": xml_schema[:100],
            "schema_type": XMLSchemaType.XSD.value,
            "schema_type_label": str(XMLSchemaType.XSD.label),
        },
    )

    response = client.get(
        reverse("workflow_public_info", kwargs={"workflow_uuid": workflow.uuid}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Show schema" not in body
    assert "Schema shared" not in body


def test_public_info_view_returns_404_when_disabled(client):
    workflow = WorkflowFactory(make_info_public=False)

    response = client.get(
        reverse("workflow_public_info", kwargs={"workflow_uuid": workflow.uuid}),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
