from __future__ import annotations

from importlib import import_module

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import CertificationActionType
from validibot.actions.constants import IntegrationActionType
from validibot.actions.forms import SignedCredentialActionForm
from validibot.actions.forms import SlackMessageActionForm
from validibot.actions.models import Action
from validibot.actions.models import ActionDefinition
from validibot.actions.models import SignedCredentialAction
from validibot.actions.models import SlackMessageAction
from validibot.actions.registry import get_action_form
from validibot.actions.registry import get_action_model
from validibot.workflows.models import WorkflowStep
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def test_seed_default_actions_creates_definitions():
    """Seeding creates the default action catalog exactly once."""
    assert ActionDefinition.objects.count() == 0

    call_command("seed_default_actions")

    definitions = ActionDefinition.objects.all()
    assert definitions.filter(action_category=ActionCategoryType.INTEGRATION).exists()
    assert definitions.filter(action_category=ActionCategoryType.CERTIFICATION).exists()
    initial_count = definitions.count()

    call_command("seed_default_actions")

    assert ActionDefinition.objects.count() == initial_count


def test_action_registry_resolves_variants():
    """The action registry resolves the renamed signed credential types."""
    assert get_action_model(IntegrationActionType.SLACK_MESSAGE) is SlackMessageAction
    assert (
        get_action_model(CertificationActionType.SIGNED_CREDENTIAL)
        is SignedCredentialAction
    )
    assert (
        get_action_form(IntegrationActionType.SLACK_MESSAGE) is SlackMessageActionForm
    )
    assert (
        get_action_form(CertificationActionType.SIGNED_CREDENTIAL)
        is SignedCredentialActionForm
    )


def test_signed_credential_action_falls_back_to_default():
    """Signed credential actions use the bundled template when none is uploaded."""
    action = SignedCredentialAction.objects.create(
        definition=ActionDefinition.objects.create(
            slug="cert-default",
            name="Credential",
            description="",
            icon="bi-award",
            action_category=ActionCategoryType.CERTIFICATION,
            type=CertificationActionType.SIGNED_CREDENTIAL,
        ),
        name="Signed credential",
        description="",
    )

    default_name = action.get_credential_template_display_name()
    assert default_name.endswith("default_signed_credential.pdf")
    default_path = action.get_credential_template_path()
    assert default_path.endswith("default_signed_credential.pdf")


def test_rename_migration_updates_legacy_step_config():
    """The rename migration rewrites old action definitions and step config keys."""
    workflow = WorkflowFactory()
    definition = ActionDefinition.objects.create(
        slug="certification-signed-certificate",
        name="Signed certificate",
        description="Issue a signed certificate for successful validations.",
        icon="bi-award",
        action_category=ActionCategoryType.CERTIFICATION,
        type="SIGNED_CERTIFICATE",
    )
    action = Action.objects.create(
        definition=definition,
        name="Legacy credential step",
        description="",
    )
    step = WorkflowStep.objects.create(
        workflow=workflow,
        action=action,
        order=10,
        name="Legacy credential step",
        description="",
        config={
            "certificate_template": "legacy.pdf",
            "preserved_key": "keep-this",
        },
    )

    migration = import_module(
        "validibot.actions.migrations.0002_rename_signed_certificate_to_credential",
    )
    migration.rename_certificate_to_credential(django_apps, None)

    definition.refresh_from_db()
    step.refresh_from_db()

    assert definition.type == "SIGNED_CREDENTIAL"
    assert definition.slug == "certification-signed-credential"
    assert definition.name == "Signed credential"
    assert definition.description == (
        "Issue a signed credential for successful validations."
    )
    assert step.config == {
        "credential_template": "legacy.pdf",
        "preserved_key": "keep-this",
    }
