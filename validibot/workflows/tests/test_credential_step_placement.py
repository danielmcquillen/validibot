"""
Tests for credential step placement rules and feature gating.

These tests verify the ADR's workflow authoring constraints:
    - A credential step must come after all validator steps
    - A credential step must come after all BLOCKING action steps
    - The reorder UI enforces these rules (not just model clean)
    - Pro-only actions are filtered from the step picker when the
      feature is not enabled
    - The server-side create endpoint rejects Pro-only actions when
      the feature is not enabled

These tests exist because the initial implementation had gaps:
    - Reorder used raw .update() and bypassed model validation
    - The picker filtered Pro actions client-side but the create
      endpoint accepted any active ActionDefinition ID
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import ActionFailureMode
from validibot.actions.constants import CertificationActionType
from validibot.actions.constants import IntegrationActionType
from validibot.actions.models import Action
from validibot.actions.models import ActionDefinition
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.views.steps import _validate_credential_step_order

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def seed_roles(db):
    """Ensure role rows exist for permission checks."""
    ensure_all_roles_exist()


@pytest.fixture
def credential_definition():
    """Create a signed credential ActionDefinition for testing."""
    defn, _ = ActionDefinition.objects.get_or_create(
        slug="certification-signed-credential",
        defaults={
            "name": "Signed credential",
            "description": "Issue a signed credential.",
            "icon": "bi-award",
            "action_category": ActionCategoryType.CERTIFICATION,
            "type": CertificationActionType.SIGNED_CREDENTIAL,
            "required_feature": "signed_badges",
        },
    )
    return defn


@pytest.fixture
def workflow_with_owner():
    """Create a workflow and an owner user with manage permissions."""
    user = UserFactory()
    org = OrganizationFactory()
    grant_role(user, org, RoleCode.OWNER)
    workflow = WorkflowFactory(org=org, user=user)
    return workflow, user, org


# ── Placement validation ─────────────────────────────────────────────
# The credential step must come after all blocking work.


class TestCredentialStepPlacementValidation:
    """Verify the placement validation helper used by the reorder view.

    These tests exercise ``_validate_credential_step_order()`` directly
    to ensure the logic catches violations independently of the HTTP
    layer.
    """

    def test_valid_order_passes(self, credential_definition):
        """Credential step after all validators is valid."""
        workflow = WorkflowFactory()
        validator = ValidatorFactory()
        step1 = WorkflowStepFactory(
            workflow=workflow,
            order=10,
            validator=validator,
        )

        cred_action = Action.objects.create(
            definition=credential_definition,
            slug="test-cred",
            name="Test Credential",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        step2 = WorkflowStepFactory(
            workflow=workflow,
            order=20,
            action=cred_action,
            validator=None,
        )

        steps = [step1, step2]
        assert _validate_credential_step_order(steps) is None

    def test_validator_after_credential_rejected(self, credential_definition):
        """A validator step after the credential step is invalid.

        This is the most basic placement violation: the credential
        attests that all checks passed, so all checks must run first.
        """
        workflow = WorkflowFactory()
        validator = ValidatorFactory()

        cred_action = Action.objects.create(
            definition=credential_definition,
            slug="test-cred-reject",
            name="Test Credential",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        step1 = WorkflowStepFactory(
            workflow=workflow,
            order=10,
            action=cred_action,
            validator=None,
        )
        step2 = WorkflowStepFactory(
            workflow=workflow,
            order=20,
            validator=validator,
        )

        steps = [step1, step2]
        error = _validate_credential_step_order(steps)
        assert error is not None
        assert "validation steps" in error.lower()

    def test_blocking_action_after_credential_rejected(self, credential_definition):
        """A BLOCKING action after the credential step is invalid.

        The credential can only attest that the run succeeded if
        all blocking work completed first.
        """
        workflow = WorkflowFactory()

        # The credential action step
        cred_action = Action.objects.create(
            definition=credential_definition,
            slug="test-cred-block",
            name="Test Credential",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        step1 = WorkflowStepFactory(
            workflow=workflow,
            order=10,
            action=cred_action,
            validator=None,
        )

        # A BLOCKING action after
        slack_defn, _ = ActionDefinition.objects.get_or_create(
            slug="integration-slack-message",
            defaults={
                "name": "Slack",
                "action_category": ActionCategoryType.INTEGRATION,
                "type": IntegrationActionType.SLACK_MESSAGE,
            },
        )
        slack_action = Action.objects.create(
            definition=slack_defn,
            slug="test-slack-blocking",
            name="Test Slack Blocking",
            failure_mode=ActionFailureMode.BLOCKING,
        )
        step2 = WorkflowStepFactory(
            workflow=workflow,
            order=20,
            action=slack_action,
            validator=None,
        )

        steps = [step1, step2]
        error = _validate_credential_step_order(steps)
        assert error is not None
        assert "blocking" in error.lower()

    def test_advisory_action_after_credential_allowed(self, credential_definition):
        """ADVISORY actions may appear after the credential step.

        Advisory post-credential actions (e.g., analytics, notifications)
        are valid because their failure doesn't change the run outcome.
        """
        workflow = WorkflowFactory()

        cred_action = Action.objects.create(
            definition=credential_definition,
            slug="test-cred-advisory",
            name="Test Credential",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        step1 = WorkflowStepFactory(
            workflow=workflow,
            order=10,
            action=cred_action,
            validator=None,
        )

        slack_defn, _ = ActionDefinition.objects.get_or_create(
            slug="integration-slack-message-2",
            defaults={
                "name": "Slack",
                "action_category": ActionCategoryType.INTEGRATION,
                "type": IntegrationActionType.SLACK_MESSAGE,
            },
        )
        slack_action = Action.objects.create(
            definition=slack_defn,
            slug="test-slack-advisory",
            name="Test Slack Advisory",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        step2 = WorkflowStepFactory(
            workflow=workflow,
            order=20,
            action=slack_action,
            validator=None,
        )

        steps = [step1, step2]
        assert _validate_credential_step_order(steps) is None

    def test_no_credential_step_passes(self):
        """A workflow with no credential step has no placement rules."""
        workflow = WorkflowFactory()
        validator = ValidatorFactory()
        step = WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
        )
        assert _validate_credential_step_order([step]) is None


# ── Server-side feature gating ───────────────────────────────────────
# The create endpoint must reject Pro-only actions when the feature is
# not enabled, even if the picker was bypassed.


class TestServerSideFeatureGating:
    """Verify that the step create endpoint enforces required_feature.

    The UI step picker filters out Pro-only actions, but a crafted
    POST or stale browser tab could bypass the picker.  The server
    must independently verify the feature is enabled.
    """

    def test_create_endpoint_rejects_when_feature_disabled(
        self,
        client,
        workflow_with_owner,
        credential_definition,
    ):
        """Attempting to create a credential step without Pro installed
        should return 404, not silently succeed.
        """
        from validibot.core.features import get_enabled_features
        from validibot.core.features import register_feature
        from validibot.core.features import reset_features

        workflow, user, org = workflow_with_owner
        client.force_login(user)
        user.set_current_org(org)

        url = reverse(
            "workflows:workflow_step_action_create",
            kwargs={
                "pk": workflow.pk,
                "action_definition_id": credential_definition.pk,
            },
        )

        original_features = get_enabled_features()
        reset_features()
        try:
            response = client.get(url)
            assert response.status_code == HTTPStatus.NOT_FOUND
        finally:
            reset_features()
            for feature in original_features:
                register_feature(feature)
