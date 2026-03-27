"""Tests for workflow deletion and tombstone safeguards.

Workflows that have issued signed credentials must not be silently deleted —
they act as historical records that external verifiers can reference.

This module tests three layers of deletion protection:

1. **HTMX delete blocking** — ordinary delete requests are refused with
   HTTP 409 Conflict when credentials exist, redirecting the user to the
   workflow detail page for context.

2. **Break-glass tombstone flow** — owners may use a separate high-friction
   delete endpoint that requires UUID confirmation, a written reason, and an
   explicit acknowledgement of consequences. The workflow is tombstoned
   (removed from normal surfaces, flagged as inactive) rather than hard-deleted,
   preserving referential integrity for historical runs and credentials.

3. **Tombstone effects** — tombstoned workflows are hidden from lists and
   launch flows, but their detail page and validation history remain accessible
   for audit and historical inspection.

These tests exist because silently removing credential-bearing workflows would
break the issuer-side explanation story even if the JWS itself remains valid.
The deletion contract is part of the signed-credential ADR's trust model.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _login_with_org(client, user, workflow):
    """Authenticate a user against the workflow's organization context."""

    user.set_current_org(workflow.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()


def test_delete_blocks_htmx_requests_for_workflows_with_credentials(
    client,
    monkeypatch,
):
    """HTMX delete attempts should be blocked when a workflow has credentials."""

    owner = UserFactory()
    workflow = WorkflowFactory(user=owner)
    grant_role(owner, workflow.org, RoleCode.OWNER)
    _login_with_org(client, owner, workflow)

    monkeypatch.setattr(
        "validibot.workflows.views.management.WorkflowDeleteView._has_issued_credentials",
        lambda _self, _workflow: True,
    )

    response = client.post(
        reverse("workflows:workflow_delete", args=[workflow.pk]),
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.headers["HX-Redirect"].endswith(
        reverse("workflows:workflow_detail", args=[workflow.pk]),
    )
    assert workflow.__class__.objects.filter(pk=workflow.pk).exists()


def test_break_glass_delete_tombstones_credential_workflow(
    client,
    monkeypatch,
):
    """Owners should be able to tombstone credential-bearing workflows."""

    owner = UserFactory()
    workflow = WorkflowFactory(user=owner)
    grant_role(owner, workflow.org, RoleCode.OWNER)
    _login_with_org(client, owner, workflow)

    monkeypatch.setattr(
        "validibot.workflows.views.management._workflow_has_issued_credentials",
        lambda _workflow: True,
    )
    monkeypatch.setattr(
        "validibot.workflows.views.management._workflow_issued_credential_count",
        lambda _workflow: 2,
    )
    monkeypatch.setattr(
        "validibot.workflows.views.management._compute_workflow_definition_hash",
        lambda _workflow: "abc123def456",
    )

    response = client.post(
        reverse("workflows:workflow_break_glass_delete", args=[workflow.pk]),
        data={
            "workflow_uuid_confirmation": str(workflow.uuid),
            "deletion_reason": "Customer requested historical removal.",
            "acknowledge_consequences": "on",
        },
    )

    workflow.refresh_from_db()

    assert response.status_code == HTTPStatus.FOUND
    assert response.headers["Location"].endswith(
        reverse("workflows:workflow_detail", args=[workflow.pk]),
    )
    assert workflow.is_tombstoned is True
    assert workflow.is_archived is True
    assert workflow.is_active is False
    assert workflow.tombstone_reason == "Customer requested historical removal."
    assert workflow.tombstone_workflow_definition_hash == "abc123def456"
    assert workflow.tombstoned_by_id == owner.id


def test_break_glass_delete_requires_owner_role(
    client,
    monkeypatch,
):
    """Org admins should not get the break-glass tombstone flow."""

    admin = UserFactory()
    workflow = WorkflowFactory(user=admin)
    grant_role(admin, workflow.org, RoleCode.ADMIN)
    _login_with_org(client, admin, workflow)

    monkeypatch.setattr(
        "validibot.workflows.views.management._workflow_has_issued_credentials",
        lambda _workflow: True,
    )

    response = client.get(
        reverse("workflows:workflow_break_glass_delete", args=[workflow.pk]),
    )

    assert response.status_code == HTTPStatus.FORBIDDEN


def test_break_glass_wrong_uuid_rejected(
    client,
    monkeypatch,
):
    """The break-glass form must reject a UUID confirmation that doesn't match.

    The form requires the user to type the exact workflow UUID to confirm
    they understand which specific workflow version they are tombstoning.
    A wrong UUID (e.g., a copy-paste error) must return the form with an
    error rather than silently proceeding.
    """
    owner = UserFactory()
    workflow = WorkflowFactory(user=owner)
    grant_role(owner, workflow.org, RoleCode.OWNER)
    _login_with_org(client, owner, workflow)

    monkeypatch.setattr(
        "validibot.workflows.views.management._workflow_has_issued_credentials",
        lambda _workflow: True,
    )
    monkeypatch.setattr(
        "validibot.workflows.views.management._workflow_issued_credential_count",
        lambda _workflow: 1,
    )
    monkeypatch.setattr(
        "validibot.workflows.views.management._compute_workflow_definition_hash",
        lambda _workflow: "abc123",
    )

    response = client.post(
        reverse("workflows:workflow_break_glass_delete", args=[workflow.pk]),
        data={
            "workflow_uuid_confirmation": "00000000-0000-0000-0000-000000000000",
            "deletion_reason": "Customer requested historical removal.",
            "acknowledge_consequences": "on",
        },
    )

    workflow.refresh_from_db()
    assert response.status_code == HTTPStatus.OK  # Form re-rendered with error
    assert workflow.is_tombstoned is False


def test_break_glass_missing_reason_rejected(
    client,
    monkeypatch,
):
    """The break-glass form must reject a submission with no deletion reason.

    A written reason is required so there is a human-readable audit trail
    explaining why a credential-bearing workflow was removed from normal
    product surfaces.  An empty reason field must fail form validation.
    """
    owner = UserFactory()
    workflow = WorkflowFactory(user=owner)
    grant_role(owner, workflow.org, RoleCode.OWNER)
    _login_with_org(client, owner, workflow)

    monkeypatch.setattr(
        "validibot.workflows.views.management._workflow_has_issued_credentials",
        lambda _workflow: True,
    )
    monkeypatch.setattr(
        "validibot.workflows.views.management._workflow_issued_credential_count",
        lambda _workflow: 1,
    )
    monkeypatch.setattr(
        "validibot.workflows.views.management._compute_workflow_definition_hash",
        lambda _workflow: "abc123",
    )

    response = client.post(
        reverse("workflows:workflow_break_glass_delete", args=[workflow.pk]),
        data={
            "workflow_uuid_confirmation": str(workflow.uuid),
            "deletion_reason": "",
            "acknowledge_consequences": "on",
        },
    )

    workflow.refresh_from_db()
    assert response.status_code == HTTPStatus.OK  # Form re-rendered with error
    assert workflow.is_tombstoned is False


def test_tombstoned_workflow_is_hidden_from_list_and_launch(
    client,
):
    """Tombstoned workflows should disappear from normal list and launch flows."""

    owner = UserFactory()
    workflow = WorkflowFactory(user=owner)
    grant_role(owner, workflow.org, RoleCode.OWNER)
    _login_with_org(client, owner, workflow)
    workflow.tombstone(
        deleted_by=owner,
        reason="Historical cleanup",
        workflow_definition_hash="deadbeef",
    )

    list_response = client.get(reverse("workflows:workflow_list"))
    launch_response = client.get(
        reverse("workflows:workflow_launch", args=[workflow.pk]),
    )

    assert workflow.name not in list_response.content.decode()
    assert launch_response.status_code == HTTPStatus.NOT_FOUND


def test_tombstoned_workflow_detail_and_validation_history_remain_accessible(
    client,
):
    """Historical detail and per-workflow run history should survive tombstoning."""

    owner = UserFactory()
    workflow = WorkflowFactory(user=owner)
    grant_role(owner, workflow.org, RoleCode.OWNER)
    _login_with_org(client, owner, workflow)
    ValidationRunFactory(workflow=workflow, org=workflow.org, user=owner)
    workflow.tombstone(
        deleted_by=owner,
        reason="Historical cleanup",
        workflow_definition_hash="deadbeef",
    )

    detail_response = client.get(
        reverse("workflows:workflow_detail", args=[workflow.pk]),
    )
    history_response = client.get(
        reverse("workflows:workflow_validation_list", args=[workflow.pk]),
    )

    assert detail_response.status_code == HTTPStatus.OK
    detail_html = detail_response.content.decode()
    assert (
        "This workflow has been tombstoned and is now a historical record."
        in detail_html
    )
    assert "Historical record of the validation and action sequence." in detail_html
    assert "deadbeef" in detail_html
    assert "Add step" not in detail_html
    assert "API access" not in detail_html
    assert history_response.status_code == HTTPStatus.OK
    assert "The runs below remain available for historical inspection." in (
        history_response.content.decode()
    )


def test_tombstoned_workflow_rejects_new_step_creation(
    client,
):
    """Tombstoned workflows must not accept new steps.

    A tombstoned workflow is a protected historical record.  Allowing
    further authoring would corrupt the attestation chain by creating
    a workflow version that never actually ran against the issued
    credentials.  The step editor must refuse to create steps on
    tombstoned workflows.
    """
    from validibot.validations.tests.factories import ValidatorFactory

    owner = UserFactory()
    workflow = WorkflowFactory(user=owner)
    grant_role(owner, workflow.org, RoleCode.OWNER)
    _login_with_org(client, owner, workflow)
    workflow.tombstone(
        deleted_by=owner,
        reason="Historical cleanup",
        workflow_definition_hash="deadbeef",
    )

    validator = ValidatorFactory()
    create_url = reverse(
        "workflows:workflow_step_create",
        args=[workflow.pk, validator.pk],
    )
    response = client.post(
        create_url,
        data={},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code in (
        HTTPStatus.NOT_FOUND,
        HTTPStatus.FORBIDDEN,
    )
    assert workflow.steps.count() == 0
