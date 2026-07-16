"""Tests for publishing and navigating the in-app Markdown help center.

The suite verifies that synced FlatPages remain reachable, use the application
layout, and expose fundamental workflow-data guidance from the help index.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.contrib.flatpages.models import FlatPage
from django.core.management import call_command
from django.test import override_settings

from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
def test_help_index_and_links_render(client):
    """Ensure all synced help pages render without 404s and index is separated."""
    org = OrganizationFactory(slug="help-org")
    user = UserFactory(orgs=[org])
    client.force_login(user)

    call_command("sync_help", clear=True)

    pages = list(FlatPage.objects.filter(url__startswith="/app/help/"))
    assert pages, "Expected flatpages to be synced from docs/help_pages"

    response = client.get("/app/help/")
    assert response.status_code == HTTPStatus.OK
    context = response.context
    assert context["index_item"] is not None
    assert all(item["section_slug"] != "index" for item in context["nav_items"])

    # Every page URL should render successfully
    for page in pages:
        page_response = client.get(page.url)
        assert page_response.status_code == HTTPStatus.OK, f"{page.url} should render"


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
def test_help_markdown_headings_render(client):
    """Render help markdown as HTML headings in the app layout."""
    org = OrganizationFactory(slug="help-org-headings")
    user = UserFactory(orgs=[org])
    client.force_login(user)

    call_command("sync_help", clear=True)

    response = client.get("/app/help/concepts/cel-expressions/")
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "<h3" in html or "<h2" in html


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
def test_help_page_includes_app_left_nav(client):
    """Show the primary app left navigation when viewing help pages."""
    org = OrganizationFactory(slug="help-org-left-nav")
    user = UserFactory(orgs=[org])
    client.force_login(user)

    call_command("sync_help", clear=True)

    response = client.get("/app/help/")
    assert response.status_code == HTTPStatus.OK
    assert 'id="app-left-nav"' in response.content.decode()


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
def test_workflow_data_overview_is_discoverable_and_complete(client):
    """Authors need an indexed overview of namespaces and artifact boundaries."""
    org = OrganizationFactory(slug="help-org-workflow-data")
    user = UserFactory(orgs=[org])
    client.force_login(user)

    call_command("sync_help", clear=True)

    index_response = client.get("/app/help/")
    assert index_response.status_code == HTTPStatus.OK
    index_html = index_response.content.decode()
    assert "How Data Flows Through a Workflow" in index_html
    assert "/app/help/concepts/workflow-data/" in index_html

    overview_response = client.get("/app/help/concepts/workflow-data/")
    assert overview_response.status_code == HTTPStatus.OK
    overview_html = overview_response.content.decode()
    assert "workflow constants" in overview_html
    assert "Values and artifacts are separate" in overview_html
    assert "Promoted signals are" in overview_html
