from __future__ import annotations

from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from validibot.projects.models import Project
from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.users.models import ensure_default_project
from validibot.users.tests.factories import MembershipFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import XMLSchemaType
from validibot.workflows.forms import EnergyPlusStepConfigForm
from validibot.workflows.forms import JsonSchemaStepConfigForm
from validibot.workflows.forms import WorkflowForm
from validibot.workflows.forms import WorkflowLaunchForm
from validibot.workflows.forms import XmlSchemaStepConfigForm
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db

BASE_DIR = Path(__file__).resolve().parents[3] / "tests" / "assets"
XML_SCHEMA_DIR = BASE_DIR / "xml" / "schemas"


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
            "allowed_file_types": [SubmissionFileType.JSON],
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


def test_workflow_form_allows_switching_projects_within_org():
    workflow = WorkflowFactory()
    workflow.user.set_current_org(workflow.org)
    second_project = ProjectFactory(org=workflow.org)

    form = WorkflowForm(
        data={
            "name": workflow.name,
            "slug": workflow.slug,
            "project": str(second_project.pk),
            "allowed_file_types": [SubmissionFileType.JSON],
            "version": workflow.version,
            "is_active": "on",
        },
        instance=workflow,
        user=workflow.user,
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["project"] == second_project


def test_workflow_form_rejects_project_from_other_org():
    workflow = WorkflowFactory()
    workflow.user.set_current_org(workflow.org)
    other_project = ProjectFactory()

    form = WorkflowForm(
        data={
            "name": workflow.name,
            "slug": workflow.slug,
            "project": str(other_project.pk),
            "allowed_file_types": [SubmissionFileType.JSON],
            "version": workflow.version,
            "is_active": "on",
        },
        instance=workflow,
        user=workflow.user,
    )
    form.fields["project"].queryset = Project.objects.filter(
        pk__in=[workflow.project_id, other_project.pk],
    )

    assert not form.is_valid()
    assert "project" in form.errors


def test_workflow_launch_form_accepts_inline_payload():
    workflow = WorkflowFactory()
    workflow.user.set_current_org(workflow.org)

    form = WorkflowLaunchForm(
        data={
            "file_type": SubmissionFileType.JSON,
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
        data={"file_type": SubmissionFileType.JSON},
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
            "file_type": SubmissionFileType.JSON,
            "payload": "{}",
        },
        files={"attachment": uploaded},
        workflow=workflow,
        user=workflow.user,
    )

    assert not form.is_valid()
    assert any("Provide inline content" in error for error in form.errors["__all__"])


def test_workflow_launch_form_rejects_invalid_metadata():
    workflow = WorkflowFactory()
    workflow.user.set_current_org(workflow.org)

    form = WorkflowLaunchForm(
        data={
            "file_type": SubmissionFileType.JSON,
            "payload": "{}",
            "metadata": "not-json",
        },
        workflow=workflow,
        user=workflow.user,
    )

    assert not form.is_valid()
    assert any(
        "Metadata must be valid JSON." in error for error in form.errors["__all__"]
    )


def test_workflow_launch_form_rejects_unsupported_content_type():
    workflow = WorkflowFactory()
    workflow.user.set_current_org(workflow.org)

    form = WorkflowLaunchForm(
        data={
            "file_type": "application/pdf",
            "payload": "{}",
        },
        workflow=workflow,
        user=workflow.user,
    )

    assert not form.is_valid()
    assert any(
        "Select a supported file type." in error for error in form.errors["__all__"]
    )


def test_workflow_launch_form_hides_selector_when_single_file_type():
    workflow = WorkflowFactory(
        allowed_file_types=[SubmissionFileType.JSON],
    )
    workflow.user.set_current_org(workflow.org)

    form = WorkflowLaunchForm(
        data={
            "file_type": SubmissionFileType.JSON,
            "payload": "{}",
        },
        workflow=workflow,
        user=workflow.user,
    )

    assert form.is_valid()
    assert form.single_file_type_label == SubmissionFileType.JSON.label
    assert form.fields["file_type"].widget.__class__.__name__ == "HiddenInput"


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
            "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
        },
        files={"schema_file": uploaded},
    )

    assert not form.is_valid()
    assert "2 MB or smaller" in form.errors["schema_file"][0]


def test_json_schema_form_requires_2020_12_declaration_for_text():
    form = JsonSchemaStepConfigForm(
        data={
            "name": "Missing schema",
            "schema_text": '{\n  "type": "object"\n}',
        },
    )

    assert not form.is_valid()
    assert any("Draft 2020-12" in error for error in form.errors["schema_text"])


def test_json_schema_form_requires_2020_12_declaration_for_files():
    payload = (
        b'{"$schema": "https://json-schema.org/draft-07/schema", "type": "object"}'
    )
    uploaded = SimpleUploadedFile(
        "schema.json",
        payload,
        content_type="application/json",
    )
    form = JsonSchemaStepConfigForm(
        data={"name": "Bad schema upload"},
        files={"schema_file": uploaded},
    )

    assert not form.is_valid()
    assert any("Draft 2020-12" in error for error in form.errors["schema_file"])


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
            "schema_type": XMLSchemaType.XSD.value,
        },
        files={"schema_file": uploaded},
    )

    assert not form.is_valid()
    assert "2 MB or smaller" in form.errors["schema_file"][0]


def _load_schema_asset(filename: str) -> str:
    return (XML_SCHEMA_DIR / filename).read_text(encoding="utf-8")


def test_xml_schema_form_detects_mismatched_relaxng_text():
    rng_schema = _load_schema_asset("product.rng")
    form = XmlSchemaStepConfigForm(
        data={
            "name": "RNG schema uploaded",
            "schema_type": XMLSchemaType.XSD.value,
            "schema_text": rng_schema,
        },
    )

    assert not form.is_valid()
    errors = form.errors.get("schema_text") or []
    assert any("Relax NG" in error for error in errors)


def test_xml_schema_form_detects_mismatched_dtd_file():
    dtd_schema = _load_schema_asset("product.dtd").encode("utf-8")
    uploaded = SimpleUploadedFile("product.dtd", dtd_schema, content_type="text/plain")
    form = XmlSchemaStepConfigForm(
        data={
            "name": "DTD upload",
            "schema_type": XMLSchemaType.XSD.value,
        },
        files={"schema_file": uploaded},
    )

    assert not form.is_valid()
    errors = form.errors.get("schema_file") or []
    assert any("Document Type Definition" in error for error in errors)


def test_xml_schema_form_accepts_matching_rng():
    rng_schema = _load_schema_asset("product.rng")
    form = XmlSchemaStepConfigForm(
        data={
            "name": "RNG schema",
            "schema_type": XMLSchemaType.RELAXNG.value,
            "schema_text": rng_schema,
        },
    )

    assert form.is_valid(), form.errors


def test_energyplus_form_blocks_simulation_checks_without_run_flag():
    form = EnergyPlusStepConfigForm(
        data={
            "name": "Energy simulation",
            "simulation_checks": ["eui-range"],
        },
    )

    assert not form.is_valid()
    assert any(
        "Enable 'Run EnergyPlus simulation' to use post-simulation checks." in error
        for error in form.errors["simulation_checks"]
    )


def test_energyplus_form_accepts_simulation_checks_when_enabled():
    form = EnergyPlusStepConfigForm(
        data={
            "name": "Energy simulation",
            "run_simulation": "on",
            "simulation_checks": ["eui-range"],
        },
    )

    assert form.is_valid(), form.errors
