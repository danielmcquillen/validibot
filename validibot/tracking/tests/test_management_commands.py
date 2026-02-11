from __future__ import annotations

import pytest
from django.core.management import call_command

from validibot.tracking import sample_data
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_seed_tracking_events_command_invokes_helper(monkeypatch):
    org = OrganizationFactory(slug="seed-org")
    UserFactory(orgs=[org])

    calls = []

    def fake_seed(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(sample_data, "seed_sample_tracking_data", fake_seed)

    call_command(
        "seed_tracking_events",
        "--org-slug",
        org.slug,
        "--days",
        "1",
        "--runs-per-day",
        "1",
        "--logins-per-day",
        "1",
    )

    assert len(calls) == 1
