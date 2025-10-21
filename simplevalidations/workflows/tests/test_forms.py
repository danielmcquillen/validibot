from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.users.models import ensure_default_project
from simplevalidations.users.tests.factories import (
    MembershipFactory,
    OrganizationFactory,
    UserFactory,
)
from simplevalidations.validations.constants import JSONSchemaVersion
from simplevalidations.validations.constants import XMLSchemaType
from simplevalidations.workflows.forms import EnergyPlusStepConfigForm
from simplevalidations.workflows.forms import JsonSchemaStepConfigForm
from simplevalidations.workflows.forms import XmlSchemaStepConfigForm
from simplevalidations.workflows.forms import WorkflowForm
from simplevalidations.workflows.forms import WorkflowLaunchForm
from simplevalidations.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def create_user_in_org():
    org = OrganizationFactory()
    user = UserFactory()
    MembershipFactory(user=user, org=org, is_active=True)
    user.set_current_org(org)
    return user, org


def test_workflow_form_limits_projects_to_current_org():
    user, org = create_user_in_org()
    default_project = ensure_default_project(org)
    extra_project = ProjectFactory(org=org)

    other_org = OrganizationFactory()
    ensure_default_project(other_org)
    ProjectFactory(org=other_org)

    form = WorkflowForm(user=user)
    project_field = form.fields["project"]

    project_ids = set(project_field.queryset.values_list("pk", flat=True))
    assert project_ids == {default_project.pk, extra_project.pk}
    assert project_field.initial == default_project.pk


def test_workflow_form_saves_selected_project():
    user, org = create_user_in_org()
    default_project = ensure_default_project(org)

    form = WorkflowForm(
        data={
            "name": "Compliance checks",
            "slug": "compliance-checks",
            "project": str(default_project.pk),
            "version": "1.0",
            "is_active": "on",
        },
        user=user,
    )

    assert form.is_valid(), form.errors

    workflow = form.save(commit=False)
    workflow.org = org
    workflow.user = user
    workflow.save()

    assert workflow.project == default_project


def test_workflow_launch_form_accepts_inline_payload():
    workflow = WorkflowFactory()
    workflow.user.set_current_org(workflow.org)

    form = WorkflowLaunchForm(
        data={
            "content_type": "application/json",
            "payload": '{"hello": "world"}',
            "metadata": '{"source": "ui"}',
        },
        workflow=workflow,
        user=workflow.user,
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["metadata"] == {"source": "ui"}


def test_workflow_launch_form_accepts_file_upload():
    workflow = WorkflowFactory()
    workflow.user.set_current_org(workflow.org)

    uploaded = SimpleUploadedFile(
        "document.json",
        b"{}",
        content_type="application/json",
    )
    form = WorkflowLaunchForm(
        data={"content_type": "application/json"},
        files={"attachment": uploaded},
        workflow=workflow,
        user=workflow.user,
    )

    assert form.is_valid(), form.errors


def test_workflow_launch_form_rejects_both_inputs():
    workflow = WorkflowFactory()
    workflow.user.set_current_org(workflow.org)

    uploaded = SimpleUploadedFile(
        "document.json",
        b"{}",
        content_type="application/json",
    )
    form = WorkflowLaunchForm(
        data={
            "content_type": "application/json",
            "payload": "{}",
        },
        files={"attachment": uploaded},
        workflow=workflow,
        user=workflow.user,
    )

    assert not form.is_valid()
    assert any(
        "Provide inline content" in error for error in form.errors["__all__"]
    )


def test_workflow_launch_form_rejects_invalid_metadata():
    workflow = WorkflowFactory()
    workflow.user.set_current_org(workflow.org)

    form = WorkflowLaunchForm(
        data={
            "content_type": "application/json",
            "payload": "{}",
            "metadata": "not-json",
        },
        workflow=workflow,
        user=workflow.user,
    )

    assert not form.is_valid()
    assert any("Metadata must be valid JSON." in error for error in form.errors["__all__"])


def test_workflow_launch_form_rejects_unsupported_content_type():
    workflow = WorkflowFactory()
    workflow.user.set_current_org(workflow.org)

    form = WorkflowLaunchForm(
        data={
            "content_type": "application/pdf",
            "payload": "{}",
        },
        workflow=workflow,
        user=workflow.user,
    )

    assert not form.is_valid()
    assert any("Select a supported content type." in error for error in form.errors["__all__"])


def test_json_schema_form_rejects_large_upload():
    big_content = b"{" * (2 * 1024 * 1024 + 1)
    uploaded = SimpleUploadedFile(
        "schema.json",
        big_content,
        content_type="application/json",
    )
    form = JsonSchemaStepConfigForm(
        data={
            "name": "Large JSON schema",
            "schema_source": "upload",
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
        },
        files={"schema_file": uploaded},
    )

    assert not form.is_valid()
    assert "2 MB or smaller" in form.errors["schema_file"][0]


def test_xml_schema_form_rejects_large_upload():
    big_content = b"<" * (2 * 1024 * 1024 + 1)
    uploaded = SimpleUploadedFile(
        "schema.xsd",
        big_content,
        content_type="application/xml",
    )
    form = XmlSchemaStepConfigForm(
        data={
            "name": "Large XML schema",
            "schema_source": "upload",
            "schema_type": XMLSchemaType.XSD.value,
        },
        files={"schema_file": uploaded},
    )

    assert not form.is_valid()
    assert "2 MB or smaller" in form.errors["schema_file"][0]


def test_energyplus_form_blocks_simulation_checks_without_run_flag():
    form = EnergyPlusStepConfigForm(
        data={
            "name": "Energy simulation",
            "simulation_checks": ["eui-range"],
        }
    )

    assert not form.is_valid()
    assert any(
        "Enable 'Run EnergyPlus simulation' to use post-simulation checks."
        in error
        for error in form.errors["simulation_checks"]
    )


def test_energyplus_form_accepts_simulation_checks_when_enabled():
    form = EnergyPlusStepConfigForm(
        data={
            "name": "Energy simulation",
            "run_simulation": "on",
            "simulation_checks": ["eui-range"],
        }
    )

    assert form.is_valid(), form.errors
