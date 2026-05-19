"""Tests for tracking management commands.

These commands seed analytics/demo data. The tests keep their model fixtures
valid as workflow contracts evolve so dashboard seed data does not drift away
from production validation rules.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command

from validibot.tracking import sample_data
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_seed_tracking_events_command_invokes_helper(monkeypatch):
    """The command should resolve seed context and delegate event creation."""
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
