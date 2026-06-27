"""Tests for the workflow list management surface.

These checks cover the list's workspace scoping, layout preferences,
archiving affordances, and version-family presentation. The workflow list is a
high-traffic authoring surface, so tests focus on preventing accidental leakage
across orgs and preventing versioned rows from being shown as independent
workflows.
"""

from __future__ import annotations

import re
from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.constants import WORKFLOW_LIST_LAYOUT_SESSION_KEY
from validibot.workflows.constants import WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY
from validibot.workflows.constants import WorkflowListLayout
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _switch_workspace(client, org_id: int, *, next_url: str):
    return client.post(
        reverse("users:organization-switch", args=[org_id]),
        data={"next": next_url},
        follow=True,
    )


def _log_in_owner(client, *, user, org) -> None:
    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()


def test_workflow_list_refreshes_on_workspace_switch(client):
    user = UserFactory()
    org_alpha = OrganizationFactory(name="Alpha Org")
    org_beta = OrganizationFactory(name="Beta Org")
    grant_role(user, org_alpha, RoleCode.OWNER)
    grant_role(user, org_beta, RoleCode.OWNER)

    WorkflowFactory(org=org_alpha, user=user, name="Alpha Workflow")
    WorkflowFactory(org=org_beta, user=user, name="Beta Workflow")

    client.force_login(user)
    list_url = reverse("workflows:workflow_list")

    response = _switch_workspace(client, org_alpha.id, next_url=list_url)
    assert response.status_code == HTTPStatus.OK
    content = response.content.decode()
    assert "Alpha Workflow" in content
    assert "Beta Workflow" not in content

    response = _switch_workspace(client, org_beta.id, next_url=list_url)
    assert response.status_code == HTTPStatus.OK
    content = response.content.decode()
    assert "Alpha Workflow" not in content
    assert "Beta Workflow" in content


def test_workflow_list_layout_persists_in_session(client):
    user = UserFactory()
    org = OrganizationFactory(name="Layout Org")
    grant_role(user, org, RoleCode.OWNER)
    WorkflowFactory(org=org, user=user, name="Layout Workflow")

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    url = reverse("workflows:workflow_list")
    response = client.get(f"{url}?layout=table")
    assert response.status_code == HTTPStatus.OK
    assert response.context["current_layout"] == WorkflowListLayout.TABLE
    assert client.session[WORKFLOW_LIST_LAYOUT_SESSION_KEY] == WorkflowListLayout.TABLE

    response = client.get(url)
    assert response.context["current_layout"] == WorkflowListLayout.TABLE


def test_grid_lists_only_latest_workflow_version_with_version_badges(client):
    """Grid cards should collapse a version family while linking each version."""

    user = UserFactory()
    org = OrganizationFactory(name="Grid Version Org")
    grant_role(user, org, RoleCode.OWNER)
    older = WorkflowFactory(
        org=org,
        user=user,
        name="Versioned Grid Workflow",
        slug="versioned-grid-workflow",
        version="1",
    )
    latest = WorkflowFactory(
        org=org,
        user=user,
        name="Versioned Grid Workflow",
        slug="versioned-grid-workflow",
        version="2",
    )
    _log_in_owner(client, user=user, org=org)

    response = client.get(f"{reverse('workflows:workflow_list')}?layout=grid")

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert f'id="workflow-card-{latest.pk}"' in html
    assert f'id="workflow-card-{older.pk}"' not in html
    assert "Versions" in html
    assert "View version 2" in html
    assert "View version 1" in html
    assert "bg-blue-lt" in html
    assert "workflow-version-badge--current" in html
    assert "workflow-version-badge--previous" in html


def test_table_lists_only_latest_workflow_version_with_version_badges(client):
    """Table rows should show one family row and a horizontal version list."""

    user = UserFactory()
    org = OrganizationFactory(name="Table Version Org")
    grant_role(user, org, RoleCode.OWNER)
    older = WorkflowFactory(
        org=org,
        user=user,
        name="Versioned Table Workflow",
        slug="versioned-table-workflow",
        version="1",
    )
    latest = WorkflowFactory(
        org=org,
        user=user,
        name="Versioned Table Workflow",
        slug="versioned-table-workflow",
        version="2",
    )
    _log_in_owner(client, user=user, org=org)

    response = client.get(f"{reverse('workflows:workflow_list')}?layout=table")

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert f'id="workflow-item-wrapper-{latest.pk}"' in html
    assert f'id="workflow-item-wrapper-{older.pk}"' not in html
    assert "Versions" in html
    assert "View version 2" in html
    assert "View version 1" in html
    assert "bg-blue-lt" in html
    assert "workflow-version-badge--current" in html
    assert "workflow-version-badge--previous" in html


def test_default_list_hides_family_when_latest_version_is_archived(client):
    """An older active row must not appear as current after v2 is archived."""

    user = UserFactory()
    org = OrganizationFactory(name="Archived Latest Org")
    grant_role(user, org, RoleCode.OWNER)
    older = WorkflowFactory(
        org=org,
        user=user,
        name="Archived Latest Workflow",
        slug="archived-latest-workflow",
        version="1",
    )
    latest = WorkflowFactory(
        org=org,
        user=user,
        name="Archived Latest Workflow",
        slug="archived-latest-workflow",
        version="2",
        is_active=False,
        is_archived=True,
    )
    _log_in_owner(client, user=user, org=org)

    list_url = reverse("workflows:workflow_list")
    response = client.get(list_url)
    html = response.content.decode()
    assert f'id="workflow-item-wrapper-{older.pk}"' not in html
    assert f'id="workflow-item-wrapper-{latest.pk}"' not in html

    response = client.get(f"{list_url}?archived=1")
    html = response.content.decode()
    assert f'id="workflow-item-wrapper-{latest.pk}"' in html
    assert f'id="workflow-item-wrapper-{older.pk}"' not in html


def test_inactive_workflow_reads_as_inactive_not_archived(client):
    """An inactive (but not archived) workflow must not be mislabeled 'archived'.

    Regression for the import bug: a non-active workflow used to show the launch
    tooltip 'This workflow is archived and cannot be launched', conflating
    inactive with archived. The launch control now distinguishes ``is_archived``
    from a merely deactivated workflow, so the user sees an accurate reason and a
    nudge to activate.
    """
    user = UserFactory()
    org = OrganizationFactory(name="Inactive Org")
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(
        org=org,
        user=user,
        name="Inactive Workflow",
        slug="inactive-workflow",
        is_active=False,
        is_archived=False,
    )
    _log_in_owner(client, user=user, org=org)

    response = client.get(reverse("workflows:workflow_list"))
    html = response.content.decode()

    assert f'id="workflow-item-wrapper-{workflow.pk}"' in html
    assert "This workflow is inactive. Activate it to launch." in html
    assert "This workflow is archived and cannot be launched" not in html


def test_workflow_delete_button_has_target_id(client):
    user = UserFactory()
    org = OrganizationFactory(name="Delete Org")
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(org=org, user=user, name="Delete Me")

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(list_url)
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    expected_id = f"workflow-item-wrapper-{workflow.pk}"
    assert f'hx-target="#{expected_id}"' in html


def test_archive_button_visible_for_owner_with_runs(client):
    user = UserFactory()
    org = OrganizationFactory(name="Archive Org")
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(org=org, user=user, name="Archive Me")
    submission = SubmissionFactory(org=org, user=user, workflow=workflow)
    ValidationRunFactory(submission=submission)

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(list_url)
    html = response.content.decode()
    archive_url = reverse("workflows:workflow_archive", args=[workflow.pk])
    assert archive_url in html


def test_archive_button_hidden_for_non_owner_author_other_workflow(client):
    owner = UserFactory()
    other = UserFactory()
    org = OrganizationFactory(name="Archive Org")
    grant_role(owner, org, RoleCode.OWNER)
    grant_role(other, org, RoleCode.AUTHOR)
    WorkflowFactory(org=org, user=owner, name="Owner Workflow")

    client.force_login(other)
    other.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(list_url)
    html = response.content.decode()
    assert "workflow_archive" not in html


def test_archived_badge_priority(client):
    user = UserFactory()
    org = OrganizationFactory()
    grant_role(user, org, RoleCode.OWNER)
    WorkflowFactory(
        org=org,
        user=user,
        name="Archived State",
        is_active=False,
        is_archived=True,
    )
    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list") + "?archived=1"
    response = client.get(list_url)
    html = response.content.decode()
    assert "Archived" in html


def test_archive_clears_agent_channels(client):
    """Archiving must clear ``x402_enabled`` + ``mcp_enabled``.

    The ``ck_workflow_x402_enabled_requires_alive_row`` constraint
    forbids the contradictory state where a row is archived but still
    claims to be published for paid anonymous (x402) access.  The
    archive view must therefore strip both agent channels in the same
    transition, mirroring what ``Workflow.tombstone()`` does for the
    harder removal.

    Without this, archiving a published x402 workflow would raise an
    IntegrityError at save time — the archive button would simply
    fail in production.
    """
    from validibot.submissions.constants import SubmissionRetention
    from validibot.workflows.constants import AgentBillingMode

    user = UserFactory()
    org = OrganizationFactory(mcp_allowed=True, x402_allowed=True)
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(
        org=org,
        user=user,
        name="Published x402 Workflow",
        is_active=True,
        is_archived=False,
        mcp_enabled=True,
        x402_enabled=True,
        agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
        agent_price_cents=10,
        input_retention=SubmissionRetention.DO_NOT_STORE,
    )
    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    url = reverse("workflows:workflow_archive", args=[workflow.pk])
    response = client.post(url, HTTP_HX_REQUEST="true")

    assert response.status_code in (HTTPStatus.OK, HTTPStatus.NO_CONTENT)
    workflow.refresh_from_db()
    # All four bits must flip atomically — partial state would
    # violate the alive-row constraint.
    assert workflow.is_archived is True
    assert workflow.is_active is False
    assert workflow.x402_enabled is False
    assert workflow.mcp_enabled is False


def test_unarchive_hx_updates_state(client):
    user = UserFactory()
    org = OrganizationFactory()
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(
        org=org,
        user=user,
        name="To Unarchive",
        is_active=False,
        is_archived=True,
    )
    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    url = reverse("workflows:workflow_archive", args=[workflow.pk])
    response = client.post(
        url,
        HTTP_HX_REQUEST="true",
        data={
            "unarchive": "1",
            "show_archived": "1",
            "layout": "grid",
        },
    )
    assert response.status_code == HTTPStatus.OK
    workflow.refresh_from_db()
    assert workflow.is_archived is False
    assert workflow.is_active is True
    html = response.content.decode()
    assert "Active" in html


def test_archived_workflow_card_shows_footer_unarchive_button(client):
    """Archived workflow cards should expose a footer unarchive button in grid view."""
    user = UserFactory()
    org = OrganizationFactory(name="Archived Card Org")
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(
        org=org,
        user=user,
        name="Archived Card Workflow",
        is_active=False,
        is_archived=True,
    )

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(f"{list_url}?archived=1&layout=grid")
    assert response.status_code == HTTPStatus.OK

    html = response.content.decode()
    card_start = html.index(f'id="workflow-card-{workflow.pk}"')
    card_html = html[card_start : card_start + 4000]

    assert 'class="btn btn-sm btn-outline-secondary me-auto"' in card_html
    assert re.search(
        (
            r'btn btn-sm btn-outline-secondary me-auto".*?'
            r'<i class="bi bi-arrow-counterclockwise me-1"></i>\s*Unarchive'
        ),
        card_html,
        re.DOTALL,
    )


def test_workflow_archive_button_rendered_when_runs_exist(client):
    user = UserFactory()
    org = OrganizationFactory(name="Archive Org")
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(org=org, user=user, name="Archive Me")
    submission = SubmissionFactory(org=org, user=user, workflow=workflow)
    ValidationRunFactory(submission=submission)

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(list_url)
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    archive_url = reverse("workflows:workflow_archive", args=[workflow.pk])
    assert archive_url in html


def test_archived_toggle_urls_are_absolute(client):
    user = UserFactory()
    org = OrganizationFactory(name="Toggle Org")
    grant_role(user, org, RoleCode.OWNER)
    WorkflowFactory(org=org, user=user, name="Toggle Workflow")

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(list_url)
    assert response.status_code == HTTPStatus.OK
    toggle_urls = response.context["archived_toggle_urls"]
    assert toggle_urls["show"].startswith(list_url)
    assert "archived=1" in toggle_urls["show"]
    assert toggle_urls["hide"].startswith(list_url)
    assert "archived=0" in toggle_urls["hide"]


def test_archive_view_updates_show_archived_preference(client):
    user = UserFactory()
    org = OrganizationFactory(name="Preference Org")
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(org=org, user=user, name="Preference Workflow")

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    archive_url = reverse("workflows:workflow_archive", args=[workflow.pk])
    response = client.post(archive_url, data={"show_archived": "1"})
    assert response.status_code in {200, 302}
    assert client.session[WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY] is True

    response = client.post(
        archive_url,
        data={
            "show_archived": "0",
            "unarchive": "1",
        },
    )
    assert response.status_code in {200, 302}
    assert client.session[WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY] is False


def test_viewer_cannot_toggle_archived(client):
    user = UserFactory()
    org = OrganizationFactory(name="Viewer Org")
    grant_role(user, org, RoleCode.WORKFLOW_VIEWER)
    WorkflowFactory(org=org, user=user, name="Visible Workflow")
    WorkflowFactory(
        org=org,
        user=user,
        name="Archived Hidden Workflow",
        is_archived=True,
        is_active=False,
    )

    client.force_login(user)
    user.set_current_org(org)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    list_url = reverse("workflows:workflow_list")
    response = client.get(f"{list_url}?archived=1")
    assert response.status_code == HTTPStatus.OK
    assert response.context["show_archived"] is False
    html = response.content.decode()
    assert "Archived Hidden Workflow" not in html
    assert "Archived toggle" not in html
