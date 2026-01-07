from __future__ import annotations

import html
import json
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from lxml import html as lxml_html

from validibot.submissions.constants import SubmissionFileType
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.models import Ruleset
from validibot.validations.models import ValidationRun
from validibot.validations.services.validation_run import ValidationRunLaunchResults
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.constants import WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db

if TYPE_CHECKING:
    from validibot.submissions.models import Submission


def _force_login_for_workflow(client, workflow, *, user=None):
    user = user or workflow.user
    has_membership = user.memberships.filter(
        org=workflow.org,
        is_active=True,
    ).exists()
    if not has_membership:
        grant_role(user, workflow.org, RoleCode.WORKFLOW_VIEWER)
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
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
    )

    body = response.content.decode()
    assert response.status_code == HTTPStatus.OK
    assert "Launch Validation" in body
    assert "workflow-launch-status-area" not in body


def test_launch_page_disables_form_without_steps(client):
    workflow = WorkflowFactory()
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)

    response = client.get(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
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

    def fake_launch(self, request, org, workflow, submission, user_id, metadata, **_):
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
        "validibot.workflows.views.ValidationRunService.launch",
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

    def fake_launch(self, request, org, workflow, submission, user_id, metadata, **_):
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
        "validibot.workflows.views.ValidationRunService.launch",
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


def test_launch_upload_flow_accepts_file_and_creates_submission(client, monkeypatch):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    asset_path = Path("tests/assets/json/example_product.json")
    payload_bytes = asset_path.read_bytes()
    uploaded = SimpleUploadedFile(
        asset_path.name,
        payload_bytes,
        content_type="application/json",
    )
    captured = {}

    def fake_launch(self, request, org, workflow, submission, user_id, metadata, **_):
        captured["submission"] = submission
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
        "validibot.workflows.views.ValidationRunService.launch",
        fake_launch,
    )

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "attachment": uploaded,
            "filename": asset_path.name,
        },
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    assert "submission" in captured
    submission: Submission = captured["submission"]
    submission.refresh_from_db()
    assert submission.original_filename == asset_path.name
    assert submission.file_type == SubmissionFileType.JSON
    assert submission.input_file
    assert submission.input_file.name
    assert '"name"' in submission.get_content()
    assert ValidationRun.objects.filter(submission=submission).count() == 1
    assert client.session[WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY] == "upload"


def test_launch_inline_flow_accepts_json_payload(client, monkeypatch):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    payload = Path("tests/assets/json/example_product.json").read_text()
    captured = {}

    def fake_launch(self, request, org, workflow, submission, user_id, metadata, **_):
        captured["submission"] = submission
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
        "validibot.workflows.views.ValidationRunService.launch",
        fake_launch,
    )

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "payload": payload,
            "filename": "inline.json",
        },
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    assert "submission" in captured
    submission: Submission = captured["submission"]
    submission.refresh_from_db()
    assert not submission.input_file.name
    assert submission.file_type == SubmissionFileType.JSON
    assert '"name"' in submission.get_content()
    assert ValidationRun.objects.filter(submission=submission).count() == 1
    assert client.session[WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY] == "paste"


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
    assert "Launch Validation" in body


def test_launch_start_requires_executor_role(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    viewer = UserFactory()
    _force_login_for_workflow(client, workflow, user=viewer)

    response = client.post(
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}),
        data={
            "file_type": SubmissionFileType.JSON,
            "payload": "{}",
        },
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert (
        "You do not have permission to run this workflow." in response.content.decode()
    )


def test_launch_toggle_sections_follow_session_preference(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    url = reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk})

    response = client.get(url)
    document = lxml_html.fromstring(response.content.decode())
    upload_section = document.xpath("//*[@data-upload-section]")[0]
    paste_section = document.xpath("//*[@data-paste-section]")[0]
    upload_button = document.xpath('//*[@data-content-mode="upload"]')[0]
    paste_button = document.xpath('//*[@data-content-mode="paste"]')[0]

    assert "d-none" not in (upload_section.classes or set())
    assert "d-none" in (paste_section.classes or set())
    assert "active" in (upload_button.classes or set())
    assert "active" not in (paste_button.classes or set())

    session = client.session
    session[WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY] = "paste"
    session.save()

    response = client.get(url)
    document = lxml_html.fromstring(response.content.decode())
    upload_section = document.xpath("//*[@data-upload-section]")[0]
    paste_section = document.xpath("//*[@data-paste-section]")[0]
    upload_button = document.xpath('//*[@data-content-mode="upload"]')[0]
    paste_button = document.xpath('//*[@data-content-mode="paste"]')[0]

    assert "d-none" in (upload_section.classes or set())
    assert "d-none" not in (paste_section.classes or set())
    assert "active" not in (upload_button.classes or set())
    assert "active" in (paste_button.classes or set())


def test_browse_files_button_targets_attachment_input(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.EXECUTOR)
    url = reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk})

    response = client.get(url)
    document = lxml_html.fromstring(response.content.decode())
    browse_label = document.xpath("//*[@data-dropzone-browse]")[0]
    attachment_input = document.xpath('//input[@name="attachment"]')[0]

    assert browse_label.tag == "label"
    assert browse_label.get("for") == attachment_input.get("id")
    assert "btn" in (browse_label.classes or set())


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
    assert hx_trigger
    assert "Workflow validation canceled" in hx_trigger


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
    assert hx_trigger
    assert "Process completed before it could be cancelled" in hx_trigger


def test_cancel_run_requires_executor_role(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    viewer = UserFactory()
    _force_login_for_workflow(client, workflow, user=viewer)
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


def test_latest_run_view_loads_most_recent_run(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    superuser = UserFactory(is_superuser=True, is_staff=True)
    grant_role(superuser, workflow.org, RoleCode.ADMIN)
    superuser.set_current_org(workflow.org)
    client.force_login(superuser)
    ValidationRunFactory(
        submission__workflow=workflow,
        submission__org=workflow.org,
        workflow=workflow,
        org=workflow.org,
        status=ValidationRunStatus.SUCCEEDED,
    )

    response = client.get(
        reverse("workflows:workflow_last_run", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "workflow-run-detail-panel" in body
    assert "Launch again" in body


def test_latest_run_view_redirects_when_no_runs_exist(client):
    workflow = WorkflowFactory()
    WorkflowStepFactory(workflow=workflow)
    superuser = UserFactory(is_superuser=True, is_staff=True)
    grant_role(superuser, workflow.org, RoleCode.ADMIN)
    superuser.set_current_org(workflow.org)
    client.force_login(superuser)

    response = client.get(
        reverse("workflows:workflow_last_run", kwargs={"pk": workflow.pk}),
    )

    assert response.status_code == HTTPStatus.FOUND
    assert (
        reverse("workflows:workflow_launch", kwargs={"pk": workflow.pk}) in response.url
    )


def test_public_info_view_accessible_when_enabled(client):
    workflow = WorkflowFactory(make_info_page_public=True)
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        slug="public-json",
    )
    schema_text = json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"sku": {"type": "string"}},
        },
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
    workflow = WorkflowFactory(make_info_page_public=False)
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.AUTHOR)

    response = client.post(
        reverse("workflows:workflow_public_info_edit", kwargs={"pk": workflow.pk}),
        data={
            "title": "Public doc",
            "content_md": "## Overview\nDetails here.",
            "make_info_page_public": "on",
        },
    )

    assert response.status_code == HTTPStatus.FOUND
    workflow.refresh_from_db()
    assert workflow.make_info_page_public is True


def test_public_visibility_toggle_updates_card(client):
    workflow = WorkflowFactory(make_info_page_public=False)
    WorkflowStepFactory(workflow=workflow)
    user = _force_login_for_workflow(client, workflow)
    grant_role(user, workflow.org, RoleCode.AUTHOR)

    response = client.post(
        reverse("workflows:workflow_public_visibility", kwargs={"pk": workflow.pk}),
        data={"make_info_page_public": "true"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    workflow.refresh_from_db()
    assert workflow.make_info_page_public is True
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
    workflow = WorkflowFactory(make_info_page_public=True)
    validator = ValidatorFactory(
        validation_type=ValidationType.XML_SCHEMA,
        slug="public-xml",
    )
    xml_schema = """<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>\n  
    <xs:element name='item' type='xs:string'/>\n</xs:schema>"""
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
    workflow = WorkflowFactory(make_info_page_public=False)

    response = client.get(
        reverse("workflow_public_info", kwargs={"workflow_uuid": workflow.uuid}),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
