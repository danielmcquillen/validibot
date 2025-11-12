from __future__ import annotations

import html
import json
from http import HTTPStatus

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

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
from simplevalidations.validations.services.validation_run import (
    ValidationRunLaunchResults,
)
from simplevalidations.validations.tests.factories import ValidationRunFactory
from simplevalidations.validations.tests.factories import ValidatorFactory
from simplevalidations.workflows.constants import WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY
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

    body = response.content.decode()
    assert response.status_code == HTTPStatus.OK
    assert "Start Validation" in body
    assert "workflow-launch-status-area" not in body


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


def test_launch_post_creates_run_and_redirects(client, monkeypatch):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    def fake_launch(self, request, org, workflow, submission, user_id, metadata, **_):  # noqa: ANN001
        run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=workflow.project,
            user=request.user,
            status=ValidationRunStatus.PENDING,
        )
        return ValidationRunLaunchResults(
            validation_run=run,
            data={"id": str(run.pk), "status": ValidationRunStatus.PENDING},
            status=HTTPStatus.ACCEPTED,
        )

    monkeypatch.setattr(
        "simplevalidations.workflows.views.ValidationRunService.launch",
        fake_launch,
    )

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "payload": "{}",
        },
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    body = response.content.decode()
    assert "workflow-run-detail-panel" in body
    assert ValidationRun.objects.filter(workflow=workflow).count() == 1
    session = client.session
    assert session[WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY] == "paste"


def test_launch_start_records_upload_preference(client, monkeypatch):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    def fake_launch(self, request, org, workflow, submission, user_id, metadata, **_):  # noqa: ANN001
        run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=workflow.project,
            user=request.user,
            status=ValidationRunStatus.PENDING,
        )
        return ValidationRunLaunchResults(
            validation_run=run,
            data={"id": str(run.pk), "status": ValidationRunStatus.PENDING},
            status=HTTPStatus.ACCEPTED,
        )

    monkeypatch.setattr(
        "simplevalidations.workflows.views.ValidationRunService.launch",
        fake_launch,
    )

    upload = SimpleUploadedFile("test.json", b"{}", content_type="application/json")

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "attachment": upload,
        },
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    session = client.session
    assert session[WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY] == "upload"


def test_launch_post_invalid_form_rerenders_page(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={"file_type": SubmissionFileType.JSON},
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Add content inline or upload a file" in body


def test_launch_start_requires_executor_role(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    _force_login_for_workflow(client, workflow)

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "payload": "{}",
        },
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert "You do not have permission to run this workflow." in response.content.decode()


def test_cancel_run_updates_status(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        status=ValidationRunStatus.RUNNING,
    )

    response = client.post(
        reverse(
            "workflows:workflow_launch_cancel",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
        HTTP_HX_REQUEST="true",
    )

    run.refresh_from_db()
    assert response.status_code == HTTPStatus.OK
    assert run.status == ValidationRunStatus.CANCELED
    hx_trigger = response.headers.get("HX-Trigger")
    assert hx_trigger and "Workflow validation canceled" in hx_trigger


def test_cancel_run_reports_completed_before_cancel(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        status=ValidationRunStatus.SUCCEEDED,
    )

    response = client.post(
        reverse(
            "workflows:workflow_launch_cancel",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    hx_trigger = response.headers.get("HX-Trigger")
    assert hx_trigger and "Process completed before it could be cancelled" in hx_trigger


def test_cancel_run_requires_executor_role(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    _force_login_for_workflow(client, workflow)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        status=ValidationRunStatus.RUNNING,
    )

    response = client.post(
        reverse(
            "workflows:workflow_launch_cancel",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN


def test_run_detail_page_shows_status_area(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        status=ValidationRunStatus.RUNNING,
    )

    response = client.get(
        reverse(
            "workflows:workflow_run_detail",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "workflow-run-detail-panel" in body
    assert "Cancel workflow" in body


def test_run_detail_page_shows_cancelled_actions(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        status=ValidationRunStatus.CANCELED,
    )

    response = client.get(
        reverse(
            "workflows:workflow_run_detail",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Back to launch" in body
    assert "View previous runs" in body


def test_run_detail_page_shows_completion_actions(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    run = ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        status=ValidationRunStatus.SUCCEEDED,
    )

    response = client.get(
        reverse(
            "workflows:workflow_run_detail",
            kwargs={"pk": workflow.pk, "run_id": run.pk},
        ),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "Launch again" in body
    assert "View full run" in body


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
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.XML,
            "payload": "<data/>",
        },
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
