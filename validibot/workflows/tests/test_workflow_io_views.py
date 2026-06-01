"""Import/export view tests: the page, the upload POST, and the download.

These cover the HTTP surface of the feature — the import page renders, a good
upload lands on the results fragment, a bad upload lands on the error fragment
(not a 500), and export streams a ``.vaf`` attachment. The serialization
correctness itself is covered by ``test_workflow_io.py``; here we verify the
view wiring, permissions, and the always-show-results flow.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from validibot.users.models import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.models import Workflow
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def _author(client):
    """Create an org + AUTHOR user (has WORKFLOW_EDIT), logged in as current org."""
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    membership = user.memberships.get(org=org)
    membership.set_roles({RoleCode.AUTHOR})
    user.set_current_org(org)
    user.refresh_from_db()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()
    return org, user


def _tabular_validator():
    return ValidatorFactory(
        validation_type=ValidationType.TABULAR,
        slug="tabular-validator",
        version=1,
        is_system=True,
        supports_assertions=True,
    )


def _darwin_core_bytes(name: str) -> bytes:
    return (Path(settings.BASE_DIR) / "tests" / "workflows" / name).read_bytes()


def test_import_page_renders_dropzone(client):
    """GET shows the import page with the upload dropzone and post target."""
    _author(client)
    response = client.get(reverse("workflows:workflow_import"))
    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "workflow-import-form" in body
    assert 'id="import-main"' in body


def test_posting_a_valid_vaf_lands_on_the_results_fragment(client):
    """A good upload imports the workflow and returns the success results.

    The always-show-results flow: even on success we render a results fragment
    (with the new workflow) rather than redirecting silently.
    """
    _author(client)
    _tabular_validator()
    upload = SimpleUploadedFile(
        "darwin_core.vaf",
        _darwin_core_bytes("darwin_core.vaf"),
        content_type="application/octet-stream",
    )

    response = client.post(reverse("workflows:workflow_import"), {"file": upload})

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "was imported" in body
    assert "Darwin Core Occurrence QA" in body
    assert "Go to workflow" in body


def test_posting_the_committed_darwin_core_json_imports_cleanly_end_to_end(client):
    """End-to-end: upload the committed darwin_core.json through the view.

    This is the full HTTP path — request → view → import service → database — for
    the *bare JSON* import, complementing the service-level fixture test in
    ``test_workflow_io.py``. It asserts the import is genuinely *clean*: the
    success fragment renders, no warnings block appears, and the workflow row
    actually lands in the importing org — active and launchable, not archived.
    """
    org, _user = _author(client)
    _tabular_validator()
    upload = SimpleUploadedFile(
        "darwin_core.json",
        _darwin_core_bytes("darwin_core.json"),
        content_type="application/json",
    )

    response = client.post(reverse("workflows:workflow_import"), {"file": upload})

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "was imported" in body
    assert "Darwin Core Occurrence QA" in body
    # "Clean" means the results page shows no warnings block.
    assert "needs your attention" not in body

    # The workflow really exists in the importing org, active and launchable.
    workflow = Workflow.objects.get(org=org, name="Darwin Core Occurrence QA")
    assert workflow.is_active is True
    assert workflow.is_archived is False
    assert workflow.steps.count() == 1
    assert workflow.steps.first().ruleset.assertions.count() == 4  # noqa: PLR2004


def test_posting_garbage_lands_on_the_error_fragment(client):
    """A non-importable file returns the error fragment (with a code), not a 500.

    Import failure is an expected outcome, so it renders inline with 'Try again'
    / 'Back to list' — never an unhandled exception.
    """
    _author(client)
    upload = SimpleUploadedFile(
        "broken.json",
        b"this is not a workflow",
        content_type="application/json",
    )

    response = client.post(reverse("workflows:workflow_import"), {"file": upload})

    assert response.status_code == HTTPStatus.OK
    body = response.content.decode()
    assert "couldn&#x27;t be imported" in body or "couldn't be imported" in body
    assert "Try again" in body


def test_import_requires_workflow_edit_permission(client):
    """A viewer-only user can't reach the import page."""
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    user.memberships.get(org=org).set_roles({RoleCode.WORKFLOW_VIEWER})
    user.set_current_org(org)
    user.refresh_from_db()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    response = client.get(reverse("workflows:workflow_import"))
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_export_is_denied_for_a_workflow_the_user_can_only_view(client):
    """Export is object-scoped to edit rights, not the user's current-org rights.

    Regression for an object-unsafe check: export used to gate on
    ``user_can_manage_workflow()`` (the user's *current* org), while the view
    resolves workflows through cross-org/guest/public access. So a user who is
    only a *viewer* of a workflow in another org could export its full definition
    by guessing its pk. Export now checks ``can_edit`` against the resolved
    workflow.
    """
    # The acting user is an author in org A (their current org)...
    _org_a, user = _author(client)
    # ...but only a VIEW-only member of org B, which owns the target workflow.
    org_b = OrganizationFactory()
    grant_role(user, org_b, RoleCode.WORKFLOW_VIEWER)
    owner_b = UserFactory(orgs=[org_b])
    workflow_b = WorkflowFactory(org=org_b, user=owner_b, name="Org B Workflow")

    response = client.get(reverse("workflows:workflow_export", args=[workflow_b.pk]))

    assert response.status_code == HTTPStatus.FORBIDDEN


def test_export_streams_a_vaf_attachment(client):
    """Export returns a .vaf download (a ZIP) for a workflow the user manages."""
    org, user = _author(client)
    validator = _tabular_validator()
    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=RulesetType.TABULAR,
        rules_text='{"fields": [{"name": "a"}], "primaryKey": "a"}',
    )
    workflow = WorkflowFactory(org=org, user=user)
    WorkflowStepFactory(
        workflow=workflow, validator=validator, ruleset=ruleset, order=10
    )

    response = client.get(reverse("workflows:workflow_export", args=[workflow.pk]))

    assert response.status_code == HTTPStatus.OK
    disposition = response.headers["Content-Disposition"]
    assert ".vaf" in disposition
    assert "attachment" in disposition
    # The body is a real ZIP archive (PK magic).
    assert response.getvalue()[:2] == b"PK"
