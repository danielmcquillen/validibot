from __future__ import annotations

import textwrap
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
from validibot.workflows.constants import WorkflowHistoryPolicy
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


def test_workflow_form_renders_allowed_file_type_examples_without_changing_values():
    """Allowed file-type hints must be display-only.

    The settings form shows extension examples next to broad file-type
    labels. Those examples are a UX affordance, but the submitted
    checkbox values still need to be the canonical ``SubmissionFileType``
    values consumed by workflow launch validation.
    """
    form = WorkflowForm()

    html = str(form["allowed_file_types"])

    assert 'value="json"' in html
    assert 'value="xml"' in html
    assert 'value="text"' in html
    assert 'value="name"' not in html
    assert (
        'class="workflow-file-type-option__examples">.txt, .csv, .ttl, .nt, .nq</span>'
    ) in html


def test_workflow_form_saves_selected_project():
    from validibot.submissions.constants import DataRetention

    user, org = create_user_in_org()
    default_project = ensure_default_project(org)

    form = WorkflowForm(
        data={
            "name": "Compliance checks",
            "slug": "compliance-checks",
            "project": str(default_project.pk),
            "allowed_file_types": [SubmissionFileType.JSON],
            "input_retention": DataRetention.DO_NOT_STORE,
            "output_retention": "STORE_30_DAYS",
            "version": "1",
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
    from validibot.submissions.constants import DataRetention

    workflow = WorkflowFactory()
    workflow.user.set_current_org(workflow.org)
    second_project = ProjectFactory(org=workflow.org)

    form = WorkflowForm(
        data={
            "name": workflow.name,
            "description_md": "Validates schema compliance.",
            "slug": workflow.slug,
            "project": str(second_project.pk),
            "allowed_file_types": [SubmissionFileType.JSON],
            "input_retention": DataRetention.DO_NOT_STORE,
            "output_retention": "STORE_30_DAYS",
            "version": workflow.version,
            "is_active": "on",
        },
        instance=workflow,
        user=workflow.user,
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["project"] == second_project
    saved_workflow = form.save()
    assert saved_workflow.get_public_info.content_md == "Validates schema compliance."


def test_workflow_form_rejects_project_from_other_org():
    """Projects from a different org should be rejected on the form."""
    from validibot.submissions.constants import DataRetention

    workflow = WorkflowFactory()
    workflow.user.set_current_org(workflow.org)
    other_project = ProjectFactory()

    form = WorkflowForm(
        data={
            "name": workflow.name,
            "slug": workflow.slug,
            "project": str(other_project.pk),
            "allowed_file_types": [SubmissionFileType.JSON],
            "input_retention": DataRetention.DO_NOT_STORE,
            "output_retention": "STORE_30_DAYS",
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


def test_workflow_form_requires_mode_when_schema_text_is_present():
    """Schema text without an explicit authoring mode should not save silently."""
    from validibot.submissions.constants import DataRetention

    user, org = create_user_in_org()
    default_project = ensure_default_project(org)

    form = WorkflowForm(
        data={
            "name": "Schema without mode",
            "slug": "schema-without-mode",
            "project": str(default_project.pk),
            "allowed_file_types": [SubmissionFileType.JSON],
            "input_schema_pydantic": textwrap.dedent(
                """\
                class ProductInput(BaseModel):
                    sku: str = Field(description="Product SKU")
                """,
            ),
            "input_retention": DataRetention.DO_NOT_STORE,
            "output_retention": "STORE_30_DAYS",
            "version": "1",
            "is_active": "on",
        },
        user=user,
    )

    assert not form.is_valid()
    assert "input_schema_mode" in form.errors
    assert any(
        "Choose JSON Schema or Pydantic before saving" in error
        for error in form.errors["input_schema_mode"]
    )


def test_workflow_form_round_trips_pydantic_source_text():
    """Saved Pydantic authoring text should repopulate on the edit form."""
    from validibot.submissions.constants import DataRetention

    user, org = create_user_in_org()
    default_project = ensure_default_project(org)
    source_text = textwrap.dedent(
        """\
        class ProductInput(BaseModel):
            sku: str = Field(description="Product SKU")
            price: float = Field(description="Unit price", ge=0, le=20)
        """,
    ).strip()

    form = WorkflowForm(
        data={
            "name": "Schema round trip",
            "slug": "schema-round-trip",
            "project": str(default_project.pk),
            "allowed_file_types": [SubmissionFileType.JSON],
            "input_schema_mode": "pydantic",
            "input_schema_pydantic": source_text,
            "input_retention": DataRetention.DO_NOT_STORE,
            "output_retention": "STORE_30_DAYS",
            "version": "1",
            "is_active": "on",
        },
        user=user,
    )

    assert form.is_valid(), form.errors

    workflow = form.save(commit=False)
    workflow.org = org
    workflow.user = user
    workflow.save()
    workflow.refresh_from_db()

    assert workflow.input_schema_source_mode == "pydantic"
    assert workflow.input_schema_source_text == source_text

    reopened_form = WorkflowForm(instance=workflow, user=user)

    assert reopened_form.fields["input_schema_mode"].initial == "pydantic"
    assert reopened_form.fields["input_schema_pydantic"].initial == source_text


def test_workflow_launch_form_accepts_inline_payload():
    """Inline JSON payloads should still validate through the launch form."""
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


# ==============================================================================
# Tests for optional submission fields (allow_submission_name, etc.)
# ==============================================================================


class TestWorkflowLaunchFormOptionalFields:
    """Tests for optional submission fields controlled by workflow settings."""

    def test_name_field_visible_when_allowed(self):
        """Name field should be visible when allow_submission_name=True."""
        workflow = WorkflowFactory(allow_submission_name=True)

        form = WorkflowLaunchForm(
            data={
                "file_type": SubmissionFileType.JSON,
                "payload": "{}",
                "filename": "my-submission",
            },
            workflow=workflow,
        )

        # Field should NOT be hidden
        assert form.fields["filename"].widget.__class__.__name__ != "HiddenInput"
        assert form.is_valid(), form.errors
        assert form.cleaned_data["filename"] == "my-submission"

    def test_name_field_hidden_when_not_allowed(self):
        """Name field should be hidden when allow_submission_name=False."""
        workflow = WorkflowFactory(allow_submission_name=False)

        form = WorkflowLaunchForm(
            data={
                "file_type": SubmissionFileType.JSON,
                "payload": "{}",
            },
            workflow=workflow,
        )

        # Field should be hidden
        assert form.fields["filename"].widget.__class__.__name__ == "HiddenInput"

    def test_name_cleared_when_not_allowed(self):
        """Name value should be cleared even if submitted when not allowed."""
        workflow = WorkflowFactory(allow_submission_name=False)

        form = WorkflowLaunchForm(
            data={
                "file_type": SubmissionFileType.JSON,
                "payload": "{}",
                "filename": "sneaky-name",  # Try to submit anyway
            },
            workflow=workflow,
        )

        assert form.is_valid(), form.errors
        # Value should be cleared by clean()
        assert form.cleaned_data["filename"] == ""

    def test_metadata_field_visible_when_allowed(self):
        """Metadata field should be visible when allow_submission_meta_data=True."""
        workflow = WorkflowFactory(allow_submission_meta_data=True)

        form = WorkflowLaunchForm(
            data={
                "file_type": SubmissionFileType.JSON,
                "payload": "{}",
                "metadata": '{"key": "value"}',
            },
            workflow=workflow,
        )

        # Field should NOT be hidden
        assert form.fields["metadata"].widget.__class__.__name__ != "HiddenInput"
        assert form.is_valid(), form.errors
        assert form.cleaned_data["metadata"] == {"key": "value"}

    def test_metadata_field_hidden_when_not_allowed(self):
        """Metadata field should be hidden when allow_submission_meta_data=False."""
        workflow = WorkflowFactory(allow_submission_meta_data=False)

        form = WorkflowLaunchForm(
            data={
                "file_type": SubmissionFileType.JSON,
                "payload": "{}",
            },
            workflow=workflow,
        )

        # Field should be hidden
        assert form.fields["metadata"].widget.__class__.__name__ == "HiddenInput"

    def test_metadata_cleared_when_not_allowed(self):
        """Metadata value should be cleared even if submitted when not allowed."""
        workflow = WorkflowFactory(allow_submission_meta_data=False)

        form = WorkflowLaunchForm(
            data={
                "file_type": SubmissionFileType.JSON,
                "payload": "{}",
                "metadata": '{"sneaky": "data"}',  # Try to submit anyway
            },
            workflow=workflow,
        )

        assert form.is_valid(), form.errors
        # Value should be empty dict
        assert form.cleaned_data["metadata"] == {}

    def test_short_description_field_visible_when_allowed(self):
        """Short description field should be visible when allowed."""
        workflow = WorkflowFactory(allow_submission_short_description=True)

        form = WorkflowLaunchForm(
            data={
                "file_type": SubmissionFileType.JSON,
                "payload": "{}",
                "short_description": "My submission description",
            },
            workflow=workflow,
        )

        # Field should NOT be hidden
        widget_name = form.fields["short_description"].widget.__class__.__name__
        assert widget_name != "HiddenInput"
        assert form.is_valid(), form.errors
        assert form.cleaned_data["short_description"] == "My submission description"

    def test_short_description_field_hidden_when_not_allowed(self):
        """Short description field should be hidden when not allowed."""
        workflow = WorkflowFactory(allow_submission_short_description=False)

        form = WorkflowLaunchForm(
            data={
                "file_type": SubmissionFileType.JSON,
                "payload": "{}",
            },
            workflow=workflow,
        )

        # Field should be hidden
        widget_name = form.fields["short_description"].widget.__class__.__name__
        assert widget_name == "HiddenInput"

    def test_short_description_cleared_when_not_allowed(self):
        """Short description should be cleared even if submitted when not allowed."""
        workflow = WorkflowFactory(allow_submission_short_description=False)

        form = WorkflowLaunchForm(
            data={
                "file_type": SubmissionFileType.JSON,
                "payload": "{}",
                "short_description": "Sneaky description",  # Try to submit anyway
            },
            workflow=workflow,
        )

        assert form.is_valid(), form.errors
        # Value should be empty string
        assert form.cleaned_data["short_description"] == ""

    def test_all_optional_fields_work_together(self):
        """All optional fields should work when all are enabled."""
        workflow = WorkflowFactory(
            allow_submission_name=True,
            allow_submission_meta_data=True,
            allow_submission_short_description=True,
        )

        form = WorkflowLaunchForm(
            data={
                "file_type": SubmissionFileType.JSON,
                "payload": '{"test": "data"}',
                "filename": "my-test-file",
                "metadata": '{"source": "test"}',
                "short_description": "Test submission for validation",
            },
            workflow=workflow,
        )

        assert form.is_valid(), form.errors
        assert form.cleaned_data["filename"] == "my-test-file"
        assert form.cleaned_data["metadata"] == {"source": "test"}
        assert form.cleaned_data["short_description"] == (
            "Test submission for validation"
        )

    def test_all_optional_fields_cleared_when_disabled(self):
        """All optional fields should be cleared when all are disabled."""
        workflow = WorkflowFactory(
            allow_submission_name=False,
            allow_submission_meta_data=False,
            allow_submission_short_description=False,
        )

        form = WorkflowLaunchForm(
            data={
                "file_type": SubmissionFileType.JSON,
                "payload": '{"test": "data"}',
                "filename": "sneaky-name",
                "metadata": '{"sneaky": "data"}',
                "short_description": "Sneaky description",
            },
            workflow=workflow,
        )

        assert form.is_valid(), form.errors
        assert form.cleaned_data["filename"] == ""
        assert form.cleaned_data["metadata"] == {}
        assert form.cleaned_data["short_description"] == ""


# ────────────────────────────────────────────────────────────────────
# WorkflowForm contract-edit gate
# ────────────────────────────────────────────────────────────────────
#
# ADR-2026-04-27 Phase 3, Session B / task 3:
# Once a workflow is locked or has runs, its launch contract is the
# rules every past run executed under. Editing those rules in place
# would silently re-write history. The form must reject contract-field
# changes and direct the operator to ``clone_to_new_version``.
#
# What counts as a "contract field" lives in
# ``validibot.workflows.services.versioning.CONTRACT_FIELDS`` and is
# enforced via ``Workflow.changed_contract_fields()``.
# ────────────────────────────────────────────────────────────────────


def _post_payload_for(workflow, **overrides):
    """Build a minimal valid POST payload for editing ``workflow``.

    Mirrors the existing form-edit helpers above. Defaults all fields
    to the workflow's current values so the test only has to specify
    *what changed*.
    """
    from validibot.submissions.constants import DataRetention

    payload = {
        "name": workflow.name,
        "slug": workflow.slug,
        "allowed_file_types": list(
            workflow.allowed_file_types or [SubmissionFileType.JSON]
        ),
        "input_retention": workflow.input_retention or DataRetention.DO_NOT_STORE,
        "output_retention": workflow.output_retention or "STORE_30_DAYS",
        "version": workflow.version,
        "history_policy": workflow.history_policy,
        "is_active": "on" if workflow.is_active else "",
    }
    payload.update(overrides)
    return payload


def test_workflow_form_blocks_narrowing_retention_on_locked_workflow():
    """Locked workflow + SHORTENED retention -> form invalid with helpful error.

    Why this matters: ``is_locked`` is the marker that a workflow's
    contract is the source of truth for past runs. Shortening retention
    from STORE_PERMANENTLY to DO_NOT_STORE would purge submissions sooner
    than past runs were promised — a real integrity breach for any
    downstream credential or audit trail that referenced them.

    Extending retention is allowed in place (covered by the
    ``allows_extending_retention`` test below) — only shortening trips
    the gate.
    """
    from validibot.submissions.constants import DataRetention

    workflow = WorkflowFactory(
        is_locked=True,
        input_retention=DataRetention.STORE_PERMANENTLY,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    workflow.user.set_current_org(workflow.org)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            input_retention=DataRetention.DO_NOT_STORE,
        ),
        instance=workflow,
        user=workflow.user,
    )

    assert not form.is_valid()
    assert "input_retention" in form.errors
    # The error message should be direction-aware — name shortening
    # specifically and point at the new-version escape hatch.
    error_text = " ".join(form.errors["input_retention"]).lower()
    assert "shorten" in error_text
    assert "new version" in error_text


def test_workflow_form_allows_extending_retention_on_locked_workflow():
    """Locked workflow + EXTENDED retention -> form valid (safe widening).

    Extending retention is a safe in-place edit: every past run was
    promised at most the old retention horizon, and the new horizon is
    longer-or-equal. No past run is invalidated by keeping data longer
    than promised. Requiring a new-version clone for this would be
    pure friction.
    """
    from validibot.submissions.constants import DataRetention

    workflow = WorkflowFactory(
        is_locked=True,
        input_retention=DataRetention.DO_NOT_STORE,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    workflow.user.set_current_org(workflow.org)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            input_retention=DataRetention.STORE_PERMANENTLY,
        ),
        instance=workflow,
        user=workflow.user,
    )

    assert form.is_valid(), form.errors


def test_workflow_form_allows_widening_file_types_on_locked_workflow():
    """Locked workflow + ADDING a file type -> form valid (safe widening).

    Adding a file type is the most common reason an author wants to
    edit a locked workflow ("I just need to also accept Plain Text").
    Past runs ran against the narrower set; the new set is a superset,
    so no past run is invalidated. Forcing a clone for this is the
    user-visible reason the original lock felt overzealous.
    """
    workflow = WorkflowFactory(
        is_locked=True,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    workflow.user.set_current_org(workflow.org)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.TEXT],
        ),
        instance=workflow,
        user=workflow.user,
    )

    assert form.is_valid(), form.errors


def test_workflow_form_superuser_bypasses_contract_lock():
    """Superuser + narrowing change on locked workflow -> form valid.

    Operational escape hatch: a superuser can apply contract-narrowing
    changes in place (e.g. removing a file type after a customer
    request). The bypass writes an audit log entry (covered by the
    next test) so the integrity story stays intact even though the
    workflow definition drifts in place.
    """
    from validibot.users.tests.factories import UserFactory

    workflow = WorkflowFactory(
        is_locked=True,
        allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.TEXT],
    )
    # Make the superuser a member of the workflow's org so the form's
    # project-clean check (which scopes projects to the user's current
    # org) doesn't trip on an unrelated membership rule.
    superuser = UserFactory(
        is_superuser=True,
        is_staff=True,
        orgs=[workflow.org],
    )
    workflow.user.set_current_org(workflow.org)
    superuser.set_current_org(workflow.org)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            allowed_file_types=[SubmissionFileType.JSON],
            # Superusers see the agent_* fields; provide the
            # required billing-mode default so the bind succeeds.
            agent_billing_mode=workflow.agent_billing_mode,
        ),
        instance=workflow,
        user=superuser,
    )

    assert form.is_valid(), form.errors


@pytest.mark.django_db
def test_workflow_form_superuser_bypass_writes_audit_entry():
    """Superuser bypass + save -> audit log records the override.

    The audit entry is what makes the bypass safe from a compliance
    angle: even though the workflow definition silently drifted, the
    audit trail names who did it, when, and which contract fields
    were affected. Searchable via ``metadata.contract_override = True``.
    """
    from validibot.audit.models import AuditLogEntry
    from validibot.users.tests.factories import UserFactory

    workflow = WorkflowFactory(
        is_locked=True,
        allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.TEXT],
    )
    # Make the superuser a member of the workflow's org so the form's
    # project-clean check (which scopes projects to the user's current
    # org) doesn't trip on an unrelated membership rule.
    superuser = UserFactory(
        is_superuser=True,
        is_staff=True,
        orgs=[workflow.org],
    )
    workflow.user.set_current_org(workflow.org)
    superuser.set_current_org(workflow.org)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            allowed_file_types=[SubmissionFileType.JSON],
            # Superusers see the agent_* fields; provide the
            # required billing-mode default so the bind succeeds.
            agent_billing_mode=workflow.agent_billing_mode,
        ),
        instance=workflow,
        user=superuser,
    )
    assert form.is_valid(), form.errors
    form.save()

    override_entries = AuditLogEntry.objects.filter(
        target_type="workflows.Workflow",
        target_id=str(workflow.pk),
        metadata__contract_override=True,
    )
    assert override_entries.exists(), (
        "Expected a contract_override audit entry after superuser bypass"
    )
    entry = override_entries.first()
    assert "allowed_file_types" in entry.metadata["fields_overridden"]


def test_workflow_form_blocks_contract_edit_on_workflow_with_runs():
    """Used workflow + changed contract field -> form invalid.

    A workflow that has even a single run is "in use" — the runs were
    launched against a specific contract and re-writing it in place
    would silently mutate the rules retroactively. Same gate as
    ``is_locked``; documents the parity explicitly.
    """
    from validibot.submissions.tests.factories import SubmissionFactory
    from validibot.validations.tests.factories import ValidationRunFactory

    workflow = WorkflowFactory(
        is_locked=False,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    workflow.user.set_current_org(workflow.org)

    # Give the workflow a run so has_runs() returns True. The factory
    # wires a Submission + ValidationRun pair appropriately.
    submission = SubmissionFactory(workflow=workflow)
    ValidationRunFactory(workflow=workflow, submission=submission)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            allowed_file_types=[SubmissionFileType.XML],
        ),
        instance=workflow,
        user=workflow.user,
    )

    assert not form.is_valid()
    assert "allowed_file_types" in form.errors


def test_workflow_form_blocks_versioned_to_mutable_with_existing_runs():
    """A used versioned workflow cannot change policy in place.

    Once a versioned workflow has runs, flipping the same row to mutable
    would let future edits alter the definition older runs point at. The
    author should clone and make the new version mutable instead.
    """
    from validibot.submissions.tests.factories import SubmissionFactory
    from validibot.validations.tests.factories import ValidationRunFactory

    workflow = WorkflowFactory(
        history_policy=WorkflowHistoryPolicy.VERSIONED,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    workflow.user.set_current_org(workflow.org)
    submission = SubmissionFactory(workflow=workflow)
    ValidationRunFactory(workflow=workflow, submission=submission)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            history_policy=WorkflowHistoryPolicy.MUTABLE,
        ),
        instance=workflow,
        user=workflow.user,
    )

    # The field is disabled on render once runs exist, so Django drops
    # any submitted change rather than surfacing a form error. The
    # workflow's history_policy stays VERSIONED.
    assert form.fields["history_policy"].disabled
    assert form.is_valid(), form.errors
    assert form.cleaned_data["history_policy"] == WorkflowHistoryPolicy.VERSIONED
    help_text = str(form.fields["history_policy"].help_text)
    assert "validation runs" in help_text
    assert "new workflow version" in help_text


def test_workflow_form_allows_versioned_to_mutable_before_runs():
    """Policy can change freely before any run guarantee has been exercised."""
    workflow = WorkflowFactory(
        history_policy=WorkflowHistoryPolicy.VERSIONED,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    workflow.user.set_current_org(workflow.org)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            history_policy=WorkflowHistoryPolicy.MUTABLE,
        ),
        instance=workflow,
        user=workflow.user,
    )

    assert form.is_valid(), form.errors


def test_workflow_form_blocks_history_policy_change_on_locked_workflow():
    """An explicit lock is also a definition boundary, even before runs exist.

    A locked workflow renders the field disabled and explains why in the
    help text. Django drops any value the user tries to submit, so the
    form validates cleanly with the original ``history_policy`` intact.
    """
    workflow = WorkflowFactory(
        is_locked=True,
        history_policy=WorkflowHistoryPolicy.VERSIONED,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    workflow.user.set_current_org(workflow.org)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            history_policy=WorkflowHistoryPolicy.MUTABLE,
        ),
        instance=workflow,
        user=workflow.user,
    )

    assert form.fields["history_policy"].disabled
    assert form.is_valid(), form.errors
    assert form.cleaned_data["history_policy"] == WorkflowHistoryPolicy.VERSIONED
    help_text = str(form.fields["history_policy"].help_text)
    assert "locked" in help_text.lower()


def test_workflow_form_blocks_mutable_to_versioned_with_existing_runs():
    """A used mutable workflow cannot be retroactively made versioned.

    Existing runs were created without the versioned-history guarantee. The
    author should clone to a fresh version and enable versioned history there
    for future runs.
    """
    from validibot.submissions.tests.factories import SubmissionFactory
    from validibot.validations.tests.factories import ValidationRunFactory

    workflow = WorkflowFactory(
        history_policy=WorkflowHistoryPolicy.MUTABLE,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    workflow.user.set_current_org(workflow.org)
    submission = SubmissionFactory(workflow=workflow)
    ValidationRunFactory(workflow=workflow, submission=submission)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            history_policy=WorkflowHistoryPolicy.VERSIONED,
        ),
        instance=workflow,
        user=workflow.user,
    )

    # Field is disabled once runs exist regardless of which direction
    # the policy would move, so the form ignores the submitted change
    # and keeps the original MUTABLE policy.
    assert form.fields["history_policy"].disabled
    assert form.is_valid(), form.errors
    assert form.cleaned_data["history_policy"] == WorkflowHistoryPolicy.MUTABLE
    help_text = str(form.fields["history_policy"].help_text)
    assert "new workflow version" in help_text


def test_workflow_form_history_policy_field_stays_enabled_for_superusers():
    """Superusers retain the existing override path on locked workflows.

    The disabled-field UX is for regular operators; superusers can
    still flip history_policy because the form's ``_clean_history_policy_lock``
    path records an audit entry when they do. Disabling the field for
    them would block legitimate operational repairs.
    """
    from validibot.submissions.tests.factories import SubmissionFactory
    from validibot.validations.tests.factories import ValidationRunFactory

    workflow = WorkflowFactory(
        history_policy=WorkflowHistoryPolicy.VERSIONED,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    superuser = workflow.user
    superuser.is_superuser = True
    superuser.save(update_fields=["is_superuser"])
    superuser.set_current_org(workflow.org)
    submission = SubmissionFactory(workflow=workflow)
    ValidationRunFactory(workflow=workflow, submission=submission)

    form = WorkflowForm(instance=workflow, user=superuser)

    assert form.fields["history_policy"].disabled is False
    # The full help text is rendered (not the locked-state variant)
    help_text = str(form.fields["history_policy"].help_text)
    assert "Versioned history is recommended" in help_text


def test_workflow_form_allows_mutable_contract_edit_with_existing_runs():
    """Mutable history permits in-place contract narrowing after runs exist.

    This pins the product meaning of the mutable policy: the workflow remains
    editable, and historical runs are records of outcomes rather than a
    reproducible evidence trail tied to immutable definitions.
    """
    from validibot.submissions.tests.factories import SubmissionFactory
    from validibot.validations.tests.factories import ValidationRunFactory

    workflow = WorkflowFactory(
        history_policy=WorkflowHistoryPolicy.MUTABLE,
        allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.TEXT],
    )
    workflow.user.set_current_org(workflow.org)
    submission = SubmissionFactory(workflow=workflow)
    ValidationRunFactory(workflow=workflow, submission=submission)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            allowed_file_types=[SubmissionFileType.JSON],
        ),
        instance=workflow,
        user=workflow.user,
    )

    assert form.is_valid(), form.errors


def test_workflow_form_allows_non_contract_edit_on_locked_workflow():
    """Locked workflow + name/description change -> form is valid.

    The gate is *contract-scoped* — the operator can still rename a
    workflow, fix typos in its description, or toggle ``is_active`` on
    a locked workflow. Without this we'd be locking the entire row,
    not just the launch contract, which is more restrictive than the
    ADR intends.
    """
    workflow = WorkflowFactory(
        is_locked=True,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    workflow.user.set_current_org(workflow.org)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            name="Renamed but contract unchanged",
        ),
        instance=workflow,
        user=workflow.user,
    )

    assert form.is_valid(), form.errors


def test_workflow_form_allows_no_op_save_on_locked_workflow():
    """Re-submitting a form with no contract changes -> still valid.

    Subtle but important: the gate must compare *proposals against
    current values*, not "is the field present?" — otherwise hitting
    Save twice on a locked workflow would falsely fail the second
    time even though nothing changed.
    """
    from validibot.submissions.constants import DataRetention

    workflow = WorkflowFactory(
        is_locked=True,
        input_retention=DataRetention.DO_NOT_STORE,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    workflow.user.set_current_org(workflow.org)

    form = WorkflowForm(
        data=_post_payload_for(workflow),
        instance=workflow,
        user=workflow.user,
    )

    assert form.is_valid(), form.errors


def test_workflow_form_allows_contract_edit_on_unused_unlocked_workflow():
    """Fresh workflow + changed contract field -> form is valid.

    The whole point of the gate is to fire only when the workflow is
    "in use" (has runs OR is locked). A fresh workflow author should
    be free to iterate on the contract until the first run lands.
    """
    from validibot.submissions.constants import DataRetention

    workflow = WorkflowFactory(
        is_locked=False,
        input_retention=DataRetention.DO_NOT_STORE,
        allowed_file_types=[SubmissionFileType.JSON],
    )
    workflow.user.set_current_org(workflow.org)

    form = WorkflowForm(
        data=_post_payload_for(
            workflow,
            input_retention=DataRetention.STORE_PERMANENTLY,
        ),
        instance=workflow,
        user=workflow.user,
    )

    assert form.is_valid(), form.errors


def test_workflow_form_blocks_allowed_file_types_set_change_on_locked():
    """Set-equality semantics: reordering doesn't trigger, real changes do.

    ``allowed_file_types`` is an ArrayField. The gate compares as sets
    so ``[JSON, XML] -> [XML, JSON]`` is a no-op (same accepted types,
    different render order). But ``[JSON]`` -> ``[XML]`` is a real
    contract change.
    """
    workflow = WorkflowFactory(
        is_locked=True,
        allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.XML],
    )
    workflow.user.set_current_org(workflow.org)

    # Reorder only — should still be valid because the *set* is unchanged.
    form_reorder = WorkflowForm(
        data=_post_payload_for(
            workflow,
            allowed_file_types=[SubmissionFileType.XML, SubmissionFileType.JSON],
        ),
        instance=workflow,
        user=workflow.user,
    )
    assert form_reorder.is_valid(), form_reorder.errors

    # Real removal — gate should fire.
    form_real_change = WorkflowForm(
        data=_post_payload_for(
            workflow,
            allowed_file_types=[SubmissionFileType.JSON],
        ),
        instance=workflow,
        user=workflow.user,
    )
    assert not form_real_change.is_valid()
    assert "allowed_file_types" in form_real_change.errors
