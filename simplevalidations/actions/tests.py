from __future__ import annotations

import pytest
from django.core.management import call_command

from simplevalidations.actions.constants import ActionCategoryType
from simplevalidations.actions.constants import CertificationActionType
from simplevalidations.actions.constants import IntegrationActionType
from simplevalidations.actions.forms import SignedCertificateActionForm
from simplevalidations.actions.forms import SlackMessageActionForm
from simplevalidations.actions.models import ActionDefinition
from simplevalidations.actions.models import SignedCertificateAction
from simplevalidations.actions.models import SlackMessageAction
from simplevalidations.actions.registry import get_action_form
from simplevalidations.actions.registry import get_action_model

pytestmark = pytest.mark.django_db


def test_seed_default_actions_creates_definitions():
    assert ActionDefinition.objects.count() == 0

    call_command("seed_default_actions")

    definitions = ActionDefinition.objects.all()
    assert definitions.filter(action_category=ActionCategoryType.INTEGRATION).exists()
    assert definitions.filter(action_category=ActionCategoryType.CERTIFICATION).exists()
    initial_count = definitions.count()

    call_command("seed_default_actions")

    assert ActionDefinition.objects.count() == initial_count


def test_action_registry_resolves_variants():
    assert get_action_model(IntegrationActionType.SLACK_MESSAGE) is SlackMessageAction
    assert (
        get_action_model(CertificationActionType.SIGNED_CERTIFICATE)
        is SignedCertificateAction
    )
    assert (
        get_action_form(IntegrationActionType.SLACK_MESSAGE) is SlackMessageActionForm
    )
    assert (
        get_action_form(CertificationActionType.SIGNED_CERTIFICATE)
        is SignedCertificateActionForm
    )


def test_signed_certificate_action_falls_back_to_default():
    action = SignedCertificateAction.objects.create(
        definition=ActionDefinition.objects.create(
            slug="cert-default",
            name="Certificate",
            description="",
            icon="bi-award",
            action_category=ActionCategoryType.CERTIFICATION,
            type=CertificationActionType.SIGNED_CERTIFICATE,
        ),
        name="Signed certificate",
        description="",
    )

    default_name = action.get_certificate_template_display_name()
    assert default_name.endswith("default_signed_certificate.pdf")
    default_path = action.get_certificate_template_path()
    assert default_path.endswith("default_signed_certificate.pdf")
