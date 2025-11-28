from __future__ import annotations

from http import HTTPStatus

import pytest
from django.contrib.flatpages.models import FlatPage
from django.core.management import call_command
from django.test import override_settings

from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory


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
    org = OrganizationFactory(slug="help-org-headings")
    user = UserFactory(orgs=[org])
    client.force_login(user)

    call_command("sync_help", clear=True)

    response = client.get("/app/help/concepts/cel-expressions/")
    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "<h3" in html or "<h2" in html
