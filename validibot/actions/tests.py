"""Tests for the action registry, descriptor sync, and provider allowlisting.

The action system has two sides that must both be correct:

1. **Python registry** — ``ActionDescriptor`` objects registered at startup
   (via ``AppConfig.ready()``) drive runtime lookup of model, form, and
   handler.  Community code owns the registry contract; installed packages
   contribute descriptors for their own action types.

2. **Database definitions** — ``ActionDefinition`` rows are synced from the
   Python registry by ``create_default_actions()`` (called from
   ``seed_default_actions`` and setup commands).  The picker modal reads
   these rows.  A Pro action is not discoverable unless both its Python
   descriptor and its ActionDefinition row exist.

3. **Provider allowlist** — to prevent silent registration of arbitrary
   third-party action plugins, the registry enforces a namespace allowlist.
   Only packages whose module names start with an official Validibot prefix
   (``validibot``, ``validibot_pro``, ``validibot_enterprise``) may register
   descriptors by default.  Operators can widen the allowlist via the
   ``VALIDIBOT_ALLOWED_ACTION_PLUGIN_PREFIXES`` setting.

These tests guard against regressions in each of those three areas.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import override_settings

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import IntegrationActionType
from validibot.actions.forms import SlackMessageActionForm
from validibot.actions.models import Action
from validibot.actions.models import ActionDefinition
from validibot.actions.models import SlackMessageAction
from validibot.actions.registry import _ensure_allowed_provider
from validibot.actions.registry import _provider_is_allowed
from validibot.actions.registry import get_action_form
from validibot.actions.registry import get_action_model
from validibot.actions.utils import create_default_actions

pytestmark = pytest.mark.django_db


def test_seed_default_actions_creates_definitions():
    """Seeding creates the registered community action catalog exactly once."""
    assert ActionDefinition.objects.count() == 0

    call_command("seed_default_actions")

    definitions = ActionDefinition.objects.all()
    assert definitions.filter(action_category=ActionCategoryType.INTEGRATION).exists()
    initial_count = definitions.count()

    call_command("seed_default_actions")

    assert ActionDefinition.objects.count() == initial_count


def test_create_default_actions_updates_existing_definition_metadata():
    """Descriptor sync should update stale metadata on existing rows."""

    definition = ActionDefinition.objects.create(
        slug="integration-slack-message",
        name="Old Slack",
        description="Old description",
        icon="bi-x",
        action_category=ActionCategoryType.INTEGRATION,
        type=IntegrationActionType.SLACK_MESSAGE,
    )

    created, updated = create_default_actions()

    assert created == []
    assert updated == [definition]

    definition.refresh_from_db()
    assert definition.name == "Slack message"
    assert definition.description == "Send a message to a Slack channel."
    assert definition.icon == "bi-slack"


def test_action_registry_resolves_variants():
    """The community action registry resolves only installed action plugins."""
    assert get_action_model(IntegrationActionType.SLACK_MESSAGE) is SlackMessageAction
    assert (
        get_action_form(IntegrationActionType.SLACK_MESSAGE) is SlackMessageActionForm
    )
    assert get_action_model("SIGNED_CREDENTIAL") is Action
    assert get_action_form("SIGNED_CREDENTIAL") is None


# ── Provider namespace allowlisting ──────────────────────────────────
# The registry rejects descriptors from unexpected module namespaces so
# that third-party packages cannot silently register action plugins.
# These tests are pure Python (no DB) and do not need django_db.


@pytest.mark.no_db
class TestProviderNamespaceAllowlist:
    """Verify that only official package namespaces may register descriptors.

    Without an allowlist, any installed Django app could register an action
    descriptor at startup and have it appear in the workflow step picker for
    all tenants.  The allowlist keeps action loading explicit and conservative.

    Self-host operators can widen the allowlist via
    ``VALIDIBOT_ALLOWED_ACTION_PLUGIN_PREFIXES`` in settings, but third-party
    packages must not appear by default.
    """

    def test_official_prefixes_are_allowed(self):
        """All official Validibot package namespaces pass the allowlist check."""
        assert _provider_is_allowed("validibot") is True
        assert _provider_is_allowed("validibot.actions") is True
        assert _provider_is_allowed("validibot_pro") is True
        assert _provider_is_allowed("validibot_pro.credentials") is True
        assert _provider_is_allowed("validibot_enterprise") is True
        assert _provider_is_allowed("validibot_enterprise.sso") is True

    def test_unknown_provider_is_rejected(self):
        """A third-party module name must not pass the allowlist check."""
        assert _provider_is_allowed("acme_corp_actions") is False
        assert _provider_is_allowed("django_actions_extra") is False

    def test_ensure_allowed_provider_raises_for_unknown_namespace(self):
        """Registering a descriptor with an unlisted provider must raise.

        This is the hard enforcement point: if a third-party package calls
        ``register_action_descriptor()`` with a provider not in the allowlist,
        the call raises ``ImproperlyConfigured`` at app startup rather than
        silently adding the action to the picker.
        """
        with pytest.raises(ImproperlyConfigured, match="not allowed"):
            _ensure_allowed_provider("acme_corp.some_plugin")

    def test_ensure_allowed_provider_is_silent_for_official_packages(self):
        """Registering an official provider must not raise."""
        _ensure_allowed_provider("validibot")
        _ensure_allowed_provider("validibot_pro.credentials")
        _ensure_allowed_provider("validibot_enterprise")

    def test_ensure_allowed_provider_is_silent_for_empty_provider(self):
        """A descriptor with no provider string should not be checked.

        Community descriptors registered without an explicit provider
        (the default) must not trigger the allowlist enforcement.
        """
        _ensure_allowed_provider("")

    @override_settings(
        VALIDIBOT_ALLOWED_ACTION_PLUGIN_PREFIXES=["validibot", "acme_corp"]
    )
    def test_allowlist_configurable_via_settings(self):
        """Operators can extend the allowlist to include their own packages.

        When ``VALIDIBOT_ALLOWED_ACTION_PLUGIN_PREFIXES`` is set in Django
        settings, the configured list is used instead of the default official
        prefixes.  This lets self-host operators add their own action plugins
        without forking the community package.
        """
        assert _provider_is_allowed("acme_corp") is True
        assert _provider_is_allowed("acme_corp.custom_actions") is True

        # The configured setting replaces — not extends — the default list,
        # so unlisted official packages are also rejected in this test.
        # (In practice, operators would include "validibot" in their list.)
        assert _provider_is_allowed("django_extra") is False
