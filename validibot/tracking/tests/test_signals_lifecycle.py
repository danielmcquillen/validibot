"""Tracking coverage for the Wave-2 activation-funnel and model-lifecycle
events.

Two capture styles are exercised because the two event families are
wired differently:

* **Async auth-funnel events** (``USER_REGISTERED`` / ``USER_EMAIL_VERIFIED``)
  follow the existing login/logout path: the receiver schedules the
  dispatch via ``transaction.on_commit``, so the test wraps it in
  ``captureOnCommitCallbacks(execute=True)`` to flush it. In the test
  deployment target the dispatcher runs inline and writes a
  ``TrackingEvent``. We call the receiver functions directly rather than
  sending the real signals so the test stays isolated from the *other*
  app receivers that also listen on ``user_signed_up`` /
  ``email_confirmed`` (workspace provisioning, guest classification, …).
* **Synchronous model-lifecycle events** (``WORKFLOW_CREATED`` /
  ``WORKFLOW_DELETED``, ``RULESET_CREATED``, ``VALIDATOR_CREATED``,
  ``SUBMISSION_CREATED``) write through ``TrackingEventService`` directly
  inside the model's ``post_save`` / ``post_delete``, so no on-commit
  flush is needed.

Each test clears the tracking table after arranging fixtures — creating
those fixtures now emits tracking events of their own — so assertions are
about exactly the event under test. Assertions filter by
``app_event_type`` for the same reason.
"""

from __future__ import annotations

from django.test import RequestFactory
from django.test import TestCase

from validibot.events.constants import AppEventType
from validibot.tracking.models import TrackingEvent
from validibot.tracking.signals import log_email_confirmed
from validibot.tracking.signals import log_user_signed_up
from validibot.users.tests.factories import UserFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory


def _clear_tracking() -> None:
    """Drop tracking rows so per-test assertions are exact.

    Fixture creation (workflows, rulesets, …) now emits tracking events;
    clearing between arrange and act keeps the assertions about the
    specific event under test.
    """

    TrackingEvent.objects.all().delete()


class AuthFunnelTrackingTests(TestCase):
    """Signup and email verification land activation-funnel events."""

    def setUp(self) -> None:
        """Each test starts from a clean tracking table."""

        self.factory = RequestFactory()
        _clear_tracking()

    def test_signup_emits_user_registered(self) -> None:
        """``user_signed_up`` is the top of the activation funnel — it must
        emit USER_REGISTERED so activation can be measured from account
        creation. The dispatch is deferred to ``on_commit`` (like login),
        hence the capture block.
        """

        user = UserFactory()
        _clear_tracking()
        request = self.factory.get("/accounts/signup/")

        with self.captureOnCommitCallbacks(execute=True):
            log_user_signed_up(sender=user.__class__, request=request, user=user)

        self.assertTrue(
            TrackingEvent.objects.filter(
                app_event_type=AppEventType.USER_REGISTERED.value,
                user=user,
            ).exists(),
        )

    def test_email_confirmed_emits_email_verified(self) -> None:
        """``email_confirmed`` carries no ``user`` kwarg, so the receiver
        resolves the owner from ``email_address.user`` and emits
        USER_EMAIL_VERIFIED (an activation checkpoint).
        """

        user = UserFactory()
        _clear_tracking()
        request = self.factory.get("/accounts/confirm-email/")

        with self.captureOnCommitCallbacks(execute=True):
            log_email_confirmed(
                sender=user.__class__,
                request=request,
                email_address=user.emailaddress_set.create(
                    email=user.email or "verify@example.com",
                    verified=True,
                    primary=True,
                ),
            )

        self.assertTrue(
            TrackingEvent.objects.filter(
                app_event_type=AppEventType.USER_EMAIL_VERIFIED.value,
                user=user,
            ).exists(),
        )


class ModelLifecycleTrackingTests(TestCase):
    """Workflow / ruleset / validator lifecycle emits analytics events."""

    def test_workflow_create_emits_event_scoped_to_org(self) -> None:
        """Creating a workflow emits WORKFLOW_CREATED carrying the org
        dimension analytics segments on.
        """

        _clear_tracking()
        workflow = WorkflowFactory()

        event = TrackingEvent.objects.filter(
            app_event_type=AppEventType.WORKFLOW_CREATED.value,
        ).first()
        self.assertIsNotNone(event)
        self.assertEqual(event.org_id, workflow.org_id)

    def test_workflow_save_does_not_emit_update_noise(self) -> None:
        """Only creation is tracked. A later save must NOT emit another
        workflow event — internal saves (counters, status) would otherwise
        drown the analytics signal. Meaningful edits are the audit log's
        job, not analytics'.
        """

        workflow = WorkflowFactory()
        _clear_tracking()

        workflow.name = "Renamed"
        workflow.save()

        self.assertFalse(
            TrackingEvent.objects.filter(
                app_event_type__startswith="workflow.",
            ).exists(),
        )

    def test_workflow_delete_emits_event(self) -> None:
        """Deleting a workflow emits WORKFLOW_DELETED — a feature-
        abandonment signal useful for churn analysis.
        """

        workflow = WorkflowFactory()
        _clear_tracking()

        workflow.delete()

        self.assertTrue(
            TrackingEvent.objects.filter(
                app_event_type=AppEventType.WORKFLOW_DELETED.value,
            ).exists(),
        )

    def test_ruleset_create_emits_event(self) -> None:
        """Configuring a rule set emits RULESET_CREATED (feature adoption)."""

        _clear_tracking()
        ruleset = RulesetFactory()

        self.assertTrue(
            TrackingEvent.objects.filter(
                app_event_type=AppEventType.RULESET_CREATED.value,
                org_id=ruleset.org_id,
            ).exists(),
        )

    def test_validator_create_emits_event(self) -> None:
        """Adding a custom validator emits VALIDATOR_CREATED."""

        _clear_tracking()
        validator = ValidatorFactory()

        self.assertTrue(
            TrackingEvent.objects.filter(
                app_event_type=AppEventType.VALIDATOR_CREATED.value,
                org_id=validator.org_id,
            ).exists(),
        )
