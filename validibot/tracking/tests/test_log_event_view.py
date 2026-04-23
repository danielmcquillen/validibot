"""
Tests for :class:`~validibot.tracking.api.log_event.LogTrackingEventView`.

The view is the worker-side receiver for
:class:`CloudTasksTrackingDispatcher`: Cloud Tasks POSTs the
serialised :class:`TrackingEventRequest` here, the view deserialises,
resolves FKs, and calls the tracking service.

Scope of these tests:

* **Payload handling** — happy path, malformed payload, orphaned
  user (deleted between enqueue and execution), and infrastructure
  failure propagation. These are the business-logic branches the
  view itself owns.

* **Not covered here**: OIDC authentication. That is extensively
  covered in ``validibot/core/tests/test_task_auth.py`` for the
  sibling ``/api/v1/execute-validation-run/`` endpoint, which uses
  exactly the same auth path. Duplicating it here would be
  redundant — the auth layer is shared across every
  ``WorkerOnlyAPIView`` subclass.

Route registration (``APP_IS_WORKER=True`` + URL present on worker
routes) is covered in
``config/tests/test_url_role_routing.py``. Here we pre-load the
worker URL config and hit the view directly with ``APIClient``.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.test import override_settings
from rest_framework.test import APIClient

from validibot.events.constants import AppEventType
from validibot.tracking.constants import TrackingEventType
from validibot.tracking.models import TrackingEvent
from validibot.users.tests.factories import UserFactory

# The worker URL path — matches WORKER_ENDPOINT_PATH in
# validibot/tracking/dispatch/cloud_tasks.py. Duplicating the literal
# here (rather than importing) means a path drift causes *two*
# tests to fail rather than one — faster diagnosis.
VIEW_URL = "/api/v1/tasks/tracking/log-event/"


@override_settings(
    APP_ROLE="worker",
    APP_IS_WORKER=True,
    # Short-circuit authentication: the view uses
    # WorkerOnlyAPIView which consults get_worker_auth_classes()
    # per request. Tests for that resolver live in test_task_auth.py.
    # Here we're testing the business-logic half of the view.
    ROOT_URLCONF="config.urls_worker",
)
class LogTrackingEventViewTests(TestCase):
    """Payload deserialisation + service dispatch behaviour."""

    def setUp(self):
        # REMOTE_ADDR=127.0.0.1 isn't special to this view, but
        # WorkerKeyAuthentication (the test-target auth backend) only
        # needs a valid header; APIClient supplies the shared-secret
        # header below.
        self.client = APIClient()
        # The test deployment target uses WorkerKeyAuthentication,
        # which checks a shared secret header. Setting WORKER_API_KEY
        # here and sending it with every request keeps the tests
        # focused on the view's behaviour rather than auth plumbing.
        self.worker_api_key = "test-worker-api-key"

    def _post(self, payload: dict) -> object:
        # WorkerKeyAuthentication expects the shared secret in the
        # Authorization header with a ``Worker-Key`` scheme — see
        # validibot/core/api/worker_auth.py for the canonical format.
        with override_settings(WORKER_API_KEY=self.worker_api_key):
            return self.client.post(
                VIEW_URL,
                data=payload,
                format="json",
                headers={"authorization": f"Worker-Key {self.worker_api_key}"},
            )

    def test_happy_path_writes_tracking_event(self):
        """A valid payload for an existing user persists a row and
        returns ``200 ok``.

        This is the production hot path: Cloud Tasks POSTs a
        login-event payload here, the view resolves the user,
        calls TrackingEventService, and returns 200 so Cloud Tasks
        considers the task complete.
        """
        user = UserFactory()
        payload = {
            "event_type": TrackingEventType.APP_EVENT,
            "app_event_type": str(AppEventType.USER_LOGGED_IN),
            "user_id": user.pk,
            "org_id": None,
            "project_id": None,
            "extra_data": {"path": "/accounts/login/"},
            "channel": "web",
        }

        response = self._post(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(
            TrackingEvent.objects.filter(
                user=user,
                app_event_type=AppEventType.USER_LOGGED_IN,
            ).count(),
            1,
        )

    def test_missing_event_type_returns_400(self):
        """A payload missing ``event_type`` is malformed — 400.

        Cloud Tasks treats 4xx as a permanent failure, so retrying
        a malformed payload is futile. We surface the error clearly
        for observability.
        """
        response = self._post({"user_id": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("event_type", response.json()["error"])
        self.assertEqual(TrackingEvent.objects.count(), 0)

    def test_deleted_user_logged_and_ack_200(self):
        """A ``user_id`` whose row has been deleted returns 200 with
        a ``skipped`` status, matching the inline dispatcher.

        Returning 500 here would cause Cloud Tasks to retry forever
        for an event that can never succeed — the user is gone.
        200 lets the task queue move on while the skip is recorded
        in Cloud Logging.
        """
        payload = {
            "event_type": TrackingEventType.APP_EVENT,
            "app_event_type": str(AppEventType.USER_LOGGED_IN),
            "user_id": 999_999_999,  # does not exist
            "extra_data": {},
            "channel": "web",
        }

        response = self._post(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "skipped_missing_user"})
        self.assertEqual(TrackingEvent.objects.count(), 0)

    def test_service_exception_returns_500(self):
        """An unexpected failure inside the service returns 500 so
        Cloud Tasks retries with backoff.

        Represents genuine infrastructure failures — DB connection
        dropped mid-request, etc. Cloud Tasks will re-deliver the
        task after a delay; the view is stateless so retry is safe.
        """
        user = UserFactory()
        payload = {
            "event_type": TrackingEventType.APP_EVENT,
            "app_event_type": str(AppEventType.USER_LOGGED_IN),
            "user_id": user.pk,
            "channel": "web",
        }

        with patch(
            "validibot.tracking.services.TrackingEventService.log_tracking_event",
            side_effect=RuntimeError("simulated DB outage"),
        ):
            response = self._post(payload)

        self.assertEqual(response.status_code, 500)
        self.assertIn("simulated DB outage", response.json()["error"])
