"""
Tests for credential step placement rules and feature gating.

These tests verify the workflow-authoring constraints:
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
from validibot.actions.constants import CredentialActionType
from validibot.actions.constants import IntegrationActionType
from validibot.actions.forms import BaseWorkflowActionForm
from validibot.actions.models import Action
from validibot.actions.models import ActionDefinition
from validibot.actions.registry import ACTION_FORM_REGISTRY
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


class StubSignedCredentialActionForm(BaseWorkflowActionForm):
    """Minimal signed-credential form used to isolate feature-gating tests."""


@pytest.fixture(autouse=True)
def seed_roles(db):
    """Ensure role rows exist for permission checks."""
    ensure_all_roles_exist()


@pytest.fixture
def credential_definition():
    """Create a signed credential ActionDefinition for testing."""
    defn, _ = ActionDefinition.objects.get_or_create(
        slug="signed-credential",
        defaults={
            "name": "Signed credential",
            "description": "Issue a signed credential.",
            "icon": "bi-award",
            "action_category": ActionCategoryType.CREDENTIAL,
            "type": CredentialActionType.SIGNED_CREDENTIAL,
            "required_commercial_feature": "signed_credentials",
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


@pytest.fixture
def registered_signed_credential_form(monkeypatch):
    """Temporarily register a minimal credential form for gating tests."""
    monkeypatch.setitem(
        ACTION_FORM_REGISTRY,
        CredentialActionType.SIGNED_CREDENTIAL,
        StubSignedCredentialActionForm,
    )


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

    def test_at_most_one_credential_step_enforced_by_model_clean(
        self, credential_definition
    ):
        """Model clean rejects a second credential step in the same workflow.

        The ADR mandates at most one SignedCredentialAction per workflow:
        having two would be semantically ambiguous (which one is the
        authoritative attestation?) and architecturally confusing.

        This test verifies the rule is caught by WorkflowStep.full_clean(),
        which is called by the step editor's form validation before saving.
        """
        from django.core.exceptions import ValidationError

        workflow = WorkflowFactory()
        cred_action_1 = Action.objects.create(
            definition=credential_definition,
            slug="test-cred-first",
            name="First Credential",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        WorkflowStepFactory(
            workflow=workflow,
            order=10,
            action=cred_action_1,
            validator=None,
        )

        cred_action_2 = Action.objects.create(
            definition=credential_definition,
            slug="test-cred-second",
            name="Second Credential",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        step2 = WorkflowStepFactory(
            workflow=workflow,
            order=20,
            action=cred_action_2,
            validator=None,
        )

        with pytest.raises(ValidationError):
            step2.full_clean()


# ── Reorder endpoint enforcement ─────────────────────────────────────
# The HTMX step-move endpoint validates placement after computing the
# proposed new order, because raw .update() bypasses model clean().


class TestReorderEndpointEnforcement:
    """Verify the HTMX move endpoint enforces credential step placement.

    The WorkflowStepMoveView uses raw ``QuerySet.update()`` for
    performance, which skips Django's model ``clean()`` method.
    Placement is therefore checked explicitly in the view before
    the update is committed.

    These tests confirm that the endpoint rejects moves that would
    produce an invalid order rather than silently persisting them.
    """

    def test_reorder_endpoint_rejects_move_that_puts_credential_before_validator(
        self,
        client,
        workflow_with_owner,
        credential_definition,
    ):
        """Moving a credential step above a validator step should return 400.

        The credential attests the final outcome of all blocking work, so
        it must stay below all validator steps.  The move endpoint must
        catch this before writing to the database.
        """
        workflow, user, org = workflow_with_owner
        client.force_login(user)
        user.set_current_org(org)

        validator = ValidatorFactory()
        WorkflowStepFactory(workflow=workflow, order=10, validator=validator)

        cred_action = Action.objects.create(
            definition=credential_definition,
            slug="test-cred-reorder",
            name="Credential",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        cred_step = WorkflowStepFactory(
            workflow=workflow,
            order=20,
            action=cred_action,
            validator=None,
        )

        move_url = reverse(
            "workflows:workflow_step_move",
            args=[workflow.pk, cred_step.pk],
        )
        response = client.post(
            move_url,
            data={"direction": "up"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == HTTPStatus.BAD_REQUEST

        # The step order must not have changed.
        cred_step.refresh_from_db()
        assert cred_step.order == 20  # noqa: PLR2004

    def test_reorder_endpoint_allows_valid_move_within_advisory_zone(
        self,
        client,
        workflow_with_owner,
        credential_definition,
    ):
        """Moving an advisory action above another advisory action is fine.

        Advisory post-credential steps may be freely reordered among
        themselves without violating placement rules.
        """
        workflow, user, org = workflow_with_owner
        client.force_login(user)
        user.set_current_org(org)

        cred_action = Action.objects.create(
            definition=credential_definition,
            slug="test-cred-valid-move",
            name="Credential",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        WorkflowStepFactory(
            workflow=workflow,
            order=10,
            action=cred_action,
            validator=None,
        )

        slack_defn, _ = ActionDefinition.objects.get_or_create(
            slug="integration-slack-message-move",
            defaults={
                "name": "Slack",
                "action_category": ActionCategoryType.INTEGRATION,
                "type": IntegrationActionType.SLACK_MESSAGE,
            },
        )
        slack_action_1 = Action.objects.create(
            definition=slack_defn,
            slug="test-slack-move-1",
            name="Slack A",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        slack_step_1 = WorkflowStepFactory(
            workflow=workflow,
            order=20,
            action=slack_action_1,
            validator=None,
        )
        slack_action_2 = Action.objects.create(
            definition=slack_defn,
            slug="test-slack-move-2",
            name="Slack B",
            failure_mode=ActionFailureMode.ADVISORY,
        )
        WorkflowStepFactory(
            workflow=workflow,
            order=30,
            action=slack_action_2,
            validator=None,
        )

        move_url = reverse(
            "workflows:workflow_step_move",
            args=[workflow.pk, slack_step_1.pk],
        )
        response = client.post(
            move_url,
            data={"direction": "down"},
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == HTTPStatus.NO_CONTENT


# ── Server-side feature gating ───────────────────────────────────────
# The create endpoint must reject Pro-only actions when the feature is
# not enabled, even if the picker was bypassed.


class TestServerSideFeatureGating:
    """Verify that the step create endpoint enforces the commercial gate.

    The UI step picker filters out Pro-only actions, but a crafted
    POST or stale browser tab could bypass the picker.  The server
    must independently verify the feature is enabled.
    """

    def test_create_endpoint_rejects_when_feature_disabled(
        self,
        client,
        workflow_with_owner,
        credential_definition,
        registered_signed_credential_form,
    ):
        """Attempting to create a credential step without Pro installed
        should return 404, not silently succeed.
        """
        from validibot.core.license import Edition
        from validibot.core.license import License
        from validibot.core.license import set_license

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

        # Force a Community license (no features). The root conftest
        # autouse fixture restores the baseline at test teardown.
        set_license(License(edition=Edition.COMMUNITY))
        response = client.get(url)
        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_create_endpoint_allows_when_feature_enabled(
        self,
        client,
        workflow_with_owner,
        credential_definition,
        registered_signed_credential_form,
    ):
        """The step create endpoint returns a form when the feature is active.

        This is the positive case for server-side feature gating: when
        ``signed_credentials`` is part of the active license (as it
        would be when validibot-pro is installed), the endpoint
        should render the credential step form rather than
        returning 404.
        """
        from validibot.core.features import CommercialFeature
        from validibot.core.license import Edition
        from validibot.core.license import License
        from validibot.core.license import set_license

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

        # Install a minimal Pro license with just the feature under
        # test. The conftest autouse fixture restores the baseline
        # at teardown.
        set_license(
            License(
                edition=Edition.PRO,
                features=frozenset(
                    {CommercialFeature.SIGNED_CREDENTIALS.value},
                ),
            ),
        )
        response = client.get(url)
        assert response.status_code == HTTPStatus.OK
        assert "Step name" in response.content.decode()
