"""
Tests for the tracking dispatcher abstraction.

Three concerns, covered in order:

1. **Registry selection** — the factory picks the right dispatcher
   for each ``DEPLOYMENT_TARGET``, and clearing the cache re-runs
   the selection. Mirrors the validation-run dispatcher tests
   (``test_task_dispatch.py``) so the two hierarchies are tested
   consistently.

2. **Dispatcher contracts** — each concrete dispatcher honours the
   "don't raise for transient failures; return a response with
   ``error`` set" rule. The signal receiver's safety net depends on
   this contract, so a dispatcher that regressed to raising would
   silently reintroduce the prod 2FA 500.

3. **Inline dispatcher service integration** — the synchronous path
   used in tests actually writes the TrackingEvent row, so end-to-end
   tests elsewhere (``test_signals.py``) keep working.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.test import override_settings

from validibot.events.constants import AppEventType
from validibot.tracking.constants import TrackingEventType
from validibot.tracking.dispatch import TrackingEventRequest
from validibot.tracking.dispatch import clear_tracking_dispatcher_cache
from validibot.tracking.dispatch import get_tracking_dispatcher
from validibot.tracking.dispatch.celery_dispatcher import CeleryTrackingDispatcher
from validibot.tracking.dispatch.cloud_tasks import CloudTasksTrackingDispatcher
from validibot.tracking.dispatch.inline import InlineTrackingDispatcher
from validibot.tracking.models import TrackingEvent
from validibot.users.tests.factories import UserFactory

# =============================================================================
# Registry selection
# =============================================================================


class TrackingDispatcherRegistryTests(TestCase):
    """The factory picks the right dispatcher per DEPLOYMENT_TARGET.

    These tests are the safety net for future deployment targets:
    adding a new ``DeploymentTarget`` without wiring a dispatcher
    here will surface as a failing test rather than a production
    ``ValueError`` at first dispatch attempt.
    """

    def setUp(self):
        clear_tracking_dispatcher_cache()

    def tearDown(self):
        clear_tracking_dispatcher_cache()

    def test_default_test_target_picks_inline(self):
        """``DEPLOYMENT_TARGET=test`` (the pytest default) selects inline.

        End-to-end tests in ``test_signals.py`` rely on this — the
        inline dispatcher writes synchronously, so TrackingEvent
        rows are visible immediately after ``on_commit`` fires.
        """
        dispatcher = get_tracking_dispatcher()
        self.assertEqual(dispatcher.dispatcher_name, "inline")
        self.assertTrue(dispatcher.is_sync)

    @override_settings(DEPLOYMENT_TARGET="local_docker_compose")
    def test_local_docker_compose_picks_celery(self):
        """Dev Docker Compose uses the Celery dispatcher.

        Same path as production Docker Compose, because both have a
        live Redis broker. Two targets, one dispatcher.
        """
        dispatcher = get_tracking_dispatcher()
        self.assertEqual(dispatcher.dispatcher_name, "celery")
        self.assertFalse(dispatcher.is_sync)

    @override_settings(DEPLOYMENT_TARGET="docker_compose")
    def test_docker_compose_picks_celery(self):
        """Prod Docker Compose uses the Celery dispatcher."""
        dispatcher = get_tracking_dispatcher()
        self.assertEqual(dispatcher.dispatcher_name, "celery")
        self.assertFalse(dispatcher.is_sync)

    @override_settings(DEPLOYMENT_TARGET="gcp")
    def test_gcp_picks_cloud_tasks(self):
        """GCP uses the Cloud Tasks dispatcher.

        This is the fix at the heart of Stage 2: before, GCP was
        silently using the Celery path (because the signal had no
        platform dispatch logic), which broke the moment a user
        with 2FA tried to log in.
        """
        dispatcher = get_tracking_dispatcher()
        self.assertEqual(dispatcher.dispatcher_name, "cloud_tasks")
        self.assertFalse(dispatcher.is_sync)

    def test_cache_returns_same_instance(self):
        """The factory caches the dispatcher per process.

        ``lru_cache`` guarantees the same object back-to-back. This
        matters because a Cloud Tasks dispatcher may hold a gRPC
        client — rebuilding it per dispatch would be wasteful.
        """
        a = get_tracking_dispatcher()
        b = get_tracking_dispatcher()
        self.assertIs(a, b)

    def test_clear_cache_forces_reselection(self):
        """``clear_tracking_dispatcher_cache`` drops the cached instance.

        Used by tests that swap ``DEPLOYMENT_TARGET`` between cases.
        Without it, the first test to run would pin the dispatcher
        for the whole session.
        """
        a = get_tracking_dispatcher()
        clear_tracking_dispatcher_cache()
        b = get_tracking_dispatcher()
        self.assertIsNot(a, b)


# =============================================================================
# Shared dispatcher-contract fixtures
# =============================================================================


def _sample_request() -> TrackingEventRequest:
    """Build a minimal, valid TrackingEventRequest for dispatcher tests.

    Keeps dispatcher tests decoupled from the signal-layer's field
    derivation (user-agent parsing, channel derivation, etc.).
    """
    return TrackingEventRequest(
        event_type=TrackingEventType.APP_EVENT,
        app_event_type=str(AppEventType.USER_LOGGED_IN),
        user_id=None,
        org_id=None,
        extra_data={"user_agent": "pytest"},
        channel="web",
    )


# =============================================================================
# InlineTrackingDispatcher — synchronous service call
# =============================================================================


class InlineTrackingDispatcherTests(TestCase):
    """The inline dispatcher writes to the DB synchronously.

    Covers the happy path and the "user has been deleted"
    orphaned-event path — which is the only externally-visible
    edge case this dispatcher has.
    """

    def setUp(self):
        self.dispatcher = InlineTrackingDispatcher()

    def test_is_always_available(self):
        """No external dependencies → always available."""
        self.assertTrue(self.dispatcher.is_available())

    def test_dispatch_writes_event(self):
        """A valid request produces a TrackingEvent row immediately."""
        user = UserFactory()
        request = TrackingEventRequest(
            event_type=TrackingEventType.APP_EVENT,
            app_event_type=str(AppEventType.USER_LOGGED_IN),
            user_id=user.pk,
            channel="web",
        )

        response = self.dispatcher.dispatch(request)

        self.assertTrue(response.is_sync)
        self.assertIsNone(response.error)
        # The row must exist synchronously — that's the whole point
        # of the inline dispatcher.
        self.assertEqual(
            TrackingEvent.objects.filter(
                user=user,
                app_event_type=AppEventType.USER_LOGGED_IN,
            ).count(),
            1,
        )

    def test_dispatch_with_deleted_user_skips_without_error(self):
        """A ``user_id`` that no longer exists is an orphaned event.

        Matches the Celery task's behaviour: log and skip, don't
        raise. The response still reports success (no ``error``)
        because the dispatch itself worked — the data just has no
        user to point at anymore.
        """
        request = TrackingEventRequest(
            event_type=TrackingEventType.APP_EVENT,
            app_event_type=str(AppEventType.USER_LOGGED_IN),
            user_id=999_999_999,  # deliberately absent
            channel="web",
        )

        response = self.dispatcher.dispatch(request)

        self.assertIsNone(response.error)
        self.assertEqual(TrackingEvent.objects.count(), 0)


# =============================================================================
# CeleryTrackingDispatcher — broker error handling
# =============================================================================


@override_settings(
    INSTALLED_APPS=[*__import__("django").conf.settings.INSTALLED_APPS],
)
class CeleryTrackingDispatcherTests(TestCase):
    """The Celery dispatcher's contracted failure modes.

    The dispatcher's availability check requires
    ``django_celery_beat`` to be installed, which it is in the
    default test settings. The tests patch the ``.delay`` call
    site so we can exercise success and broker-failure paths
    without a real broker.
    """

    def setUp(self):
        self.dispatcher = CeleryTrackingDispatcher()

    def test_dispatch_broker_error_returns_error_response(self):
        """Broker unreachable → response with ``error`` set, no raise.

        This is the specific failure that broke prod 2FA on GCP.
        The dispatcher catches it, logs, and surfaces the error
        through the return value — the signal receiver's safety
        net is the last line of defence, not the first.
        """
        with patch(
            "validibot.tracking.tasks.log_tracking_event_task.delay",
            side_effect=ConnectionError("Error 111: broker refused"),
        ):
            response = self.dispatcher.dispatch(_sample_request())

        self.assertIsNotNone(response.error)
        self.assertIn("Error 111", response.error)
        self.assertFalse(response.is_sync)
        self.assertIsNone(response.task_id)

    def test_dispatch_success_returns_task_id(self):
        """A successful enqueue surfaces the Celery task id."""
        fake_result = type("FakeAsyncResult", (), {"id": "celery-uuid-abc123"})()
        with patch(
            "validibot.tracking.tasks.log_tracking_event_task.delay",
            return_value=fake_result,
        ):
            response = self.dispatcher.dispatch(_sample_request())

        self.assertIsNone(response.error)
        self.assertFalse(response.is_sync)
        self.assertEqual(response.task_id, "celery-uuid-abc123")


# =============================================================================
# CloudTasksTrackingDispatcher — config + transport contract
# =============================================================================


class CloudTasksTrackingDispatcherTests(TestCase):
    """The Cloud Tasks dispatcher's contracted failure modes.

    The real dispatch involves a gRPC call to Google; these tests
    cover everything around that call (config validation, error
    translation) without requiring GCP credentials or a network.
    """

    def setUp(self):
        self.dispatcher = CloudTasksTrackingDispatcher()

    @override_settings(
        GCP_PROJECT_ID="",
        GCS_TASK_QUEUE_NAME="",
        WORKER_URL="",
        CLOUD_TASKS_SERVICE_ACCOUNT="",
    )
    def test_is_available_false_when_config_missing(self):
        """``is_available`` flags the misconfiguration early."""
        self.assertFalse(self.dispatcher.is_available())

    @override_settings(
        GCP_PROJECT_ID="test-project",
        GCS_TASK_QUEUE_NAME="test-queue",
        WORKER_URL="https://worker.example.test",
        CLOUD_TASKS_SERVICE_ACCOUNT="sa@test-project.iam.gserviceaccount.com",
    )
    def test_is_available_true_when_fully_configured(self):
        self.assertTrue(self.dispatcher.is_available())

    @override_settings(
        GCP_PROJECT_ID="",
        GCS_TASK_QUEUE_NAME="test-queue",
        WORKER_URL="https://worker.example.test",
        CLOUD_TASKS_SERVICE_ACCOUNT="sa@test-project.iam.gserviceaccount.com",
    )
    def test_dispatch_missing_config_returns_error_response(self):
        """Partial configuration produces a descriptive error
        response rather than a crash inside google-cloud-tasks.
        """
        response = self.dispatcher.dispatch(_sample_request())

        self.assertIsNotNone(response.error)
        self.assertIn("misconfigured", response.error.lower())
        self.assertIsNone(response.task_id)

    @override_settings(
        GCP_PROJECT_ID="test-project",
        GCS_TASK_QUEUE_NAME="test-queue",
        WORKER_URL="https://worker.example.test",
        CLOUD_TASKS_SERVICE_ACCOUNT="sa@test-project.iam.gserviceaccount.com",
        GCP_REGION="australia-southeast1",
    )
    def test_dispatch_client_exception_returns_error_response(self):
        """A Cloud Tasks API error (auth, quota, invalid payload)
        surfaces as an error response — the exception is caught and
        logged inside the dispatcher, not propagated.
        """
        with patch(
            "google.cloud.tasks_v2.CloudTasksClient",
            side_effect=RuntimeError("simulated Cloud Tasks client failure"),
        ):
            response = self.dispatcher.dispatch(_sample_request())

        self.assertIsNotNone(response.error)
        self.assertIn("simulated Cloud Tasks client failure", response.error)
        self.assertIsNone(response.task_id)

    @override_settings(
        GCP_PROJECT_ID="test-project",
        GCS_TASK_QUEUE_NAME="test-queue",
        WORKER_URL="https://worker.example.test/",  # trailing slash
        CLOUD_TASKS_SERVICE_ACCOUNT="sa@test-project.iam.gserviceaccount.com",
        GCP_REGION="australia-southeast1",
    )
    def test_dispatch_builds_endpoint_url_without_double_slash(self):
        """WORKER_URL with a trailing slash must not produce
        ``https://.../api/v1/tasks//tracking/log-event/``.

        Real deployments do both forms (Cloud Run URLs have no
        trailing slash; operators sometimes add one in settings).
        The dispatcher strips the trailing slash before
        concatenating.
        """
        captured_urls: list[str] = []

        class _FakeClient:
            def create_task(self, request):
                # The HttpRequest is nested inside the task spec
                url = request.task.http_request.url
                captured_urls.append(url)
                fake = type("FakeTask", (), {"name": "projects/x/tasks/y"})()
                return fake

        with patch(
            "google.cloud.tasks_v2.CloudTasksClient",
            return_value=_FakeClient(),
        ):
            response = self.dispatcher.dispatch(_sample_request())

        self.assertIsNone(response.error)
        self.assertEqual(len(captured_urls), 1)
        url = captured_urls[0]
        self.assertNotIn("//api", url)
        self.assertTrue(url.endswith("/api/v1/tasks/tracking/log-event/"))


# =============================================================================
# Payload round-trip
# =============================================================================


def test_tracking_event_request_payload_is_json_serialisable():
    """``TrackingEventRequest.to_payload`` must return pure primitives.

    Cloud Tasks ultimately serialises the payload as JSON for the
    HTTP POST body — a non-serialisable field (datetime, enum, ORM
    instance) would fail at dispatch time. Covering this once at the
    dataclass level catches regressions before they reach real GCP.
    """
    import json

    user_pk = 42
    org_pk = 7
    request = TrackingEventRequest(
        event_type=TrackingEventType.APP_EVENT,
        app_event_type=str(AppEventType.USER_LOGGED_IN),
        user_id=user_pk,
        org_id=org_pk,
        project_id=None,
        extra_data={"user_agent": "pytest", "path": "/accounts/login/"},
        channel="web",
    )

    # round-trip via json to prove serialisability
    encoded = json.dumps(request.to_payload())
    decoded = json.loads(encoded)

    assert decoded["event_type"] == TrackingEventType.APP_EVENT
    assert decoded["app_event_type"] == str(AppEventType.USER_LOGGED_IN)
    assert decoded["user_id"] == user_pk
    assert decoded["extra_data"]["path"] == "/accounts/login/"


# =============================================================================
# Worker-side view (LogTrackingEventView)
# =============================================================================
# Handled in a separate test module because exercising the view
# requires APP_IS_WORKER=True, which is easier to toggle cleanly in a
# dedicated test file.
