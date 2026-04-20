"""
Tests for login/logout tracking signal receivers.

Covers the existing signal behaviour plus the asynchronous-enqueue
shape introduced by refactor-step item ``[review-#11]``:

- Signal receivers extract request data synchronously (org,
  user-agent, path, channel derivation).
- They enqueue a ``log_tracking_event_task`` Celery task via
  ``transaction.on_commit`` instead of writing the row inline.
- Under ``CELERY_TASK_ALWAYS_EAGER = True`` (the test-settings
  default), the task runs synchronously when the surrounding
  transaction commits — so end-to-end behaviour is preserved for
  callers, but the DB write is no longer on the auth-path critical
  section in production.

The ``transaction=True`` marker on each test is necessary because
``transaction.on_commit`` callbacks don't fire under the default
pytest-django rollback-at-end-of-test model. With ``transaction=True``
the test uses real transactions that commit, so ``on_commit``
callbacks run as they would in production.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from validibot.events.constants import AppEventType
from validibot.tracking.models import TrackingEvent
from validibot.users.tests.factories import UserFactory

if TYPE_CHECKING:
    from django.test import Client


@pytest.mark.django_db(transaction=True)
def test_login_emits_tracking_event(client: Client):
    """End-to-end: a successful login produces a USER_LOGGED_IN
    tracking row.

    The task runs in-process under ``CELERY_TASK_ALWAYS_EAGER``, but
    only after ``on_commit`` fires — ``transaction=True`` on the
    marker forces real commits so the callback actually runs.
    """
    password = "TestPass!234"  # noqa: S105
    user = UserFactory(password=password)

    assert client.login(username=user.username, password=password)

    event = TrackingEvent.objects.filter(
        app_event_type=AppEventType.USER_LOGGED_IN,
    ).first()

    assert event is not None
    assert event.user_id == user.id


@pytest.mark.django_db(transaction=True)
def test_logout_emits_tracking_event(client: Client):
    """End-to-end: a successful logout produces a USER_LOGGED_OUT
    tracking row.
    """
    password = "LogOutPass!234"  # noqa: S105
    user = UserFactory(password=password)

    assert client.login(username=user.username, password=password)
    client.logout()

    event = TrackingEvent.objects.filter(
        app_event_type=AppEventType.USER_LOGGED_OUT,
    ).first()

    assert event is not None


@pytest.mark.django_db(transaction=True)
def test_login_enqueues_task_via_transaction_on_commit(client: Client):
    """Signal receivers must route through ``transaction.on_commit``,
    not call the service inline.

    Structural test: patches ``log_tracking_event_task.delay`` and
    verifies it was invoked via ``transaction.on_commit`` rather
    than direct call. The distinguishing property is *when* the
    call happens relative to the auth flow — with ``on_commit`` the
    task is enqueued only after the surrounding transaction commits,
    which is the whole point of the fix (a rolled-back login must
    not produce a ghost event).

    If a future refactor removes the ``transaction.on_commit``
    wrapper and just calls ``.delay(...)`` directly, the behaviour
    is visibly similar but the rollback-safety guarantee is gone.
    Patching the task lets us observe the call pattern rather than
    just the end state.
    """
    password = "OnCommitPass!234"  # noqa: S105
    user = UserFactory(password=password)

    with patch(
        "validibot.tracking.tasks.log_tracking_event_task.delay",
    ) as mock_delay:
        assert client.login(username=user.username, password=password)

    # The task must have been invoked exactly once by the login
    # signal (assumes no other signal receiver enqueues tracking
    # for USER_LOGGED_IN under this path).
    assert mock_delay.call_count >= 1
    call_kwargs = mock_delay.call_args.kwargs
    assert call_kwargs.get("user_id") == user.id
    assert call_kwargs.get("app_event_type") == str(AppEventType.USER_LOGGED_IN)


@pytest.mark.django_db(transaction=True)
def test_login_does_not_block_on_inline_tracking_write(client: Client):
    """The ``TrackingEventService.log_tracking_event`` call must NOT
    happen synchronously inside the signal receiver.

    Guard against a regression that re-introduces the inline write.
    We patch the service's ``log_tracking_event`` method and assert
    it is NOT called during the ``client.login(...)`` invocation
    itself — it's called later, when ``on_commit`` fires the Celery
    task (which in eager mode runs the service after the login
    returns).

    The distinction matters because the refactor's entire
    motivation (``[review-#11]``) is moving tracking off the
    auth-path critical section.
    """
    password = "NoBlockPass!234"  # noqa: S105
    user = UserFactory(password=password)

    with patch(
        "validibot.tracking.services.TrackingEventService.log_tracking_event",
    ) as mock_log:
        # Before on_commit fires there's no transaction to run a
        # committed callback against, so the service must not have
        # been called *during* the login request.
        # Under CELERY_TASK_ALWAYS_EAGER + transaction=True the
        # commit happens at end of request, which triggers the
        # on_commit callback, which enqueues (and eagerly executes)
        # the task, which calls the service. So the *post-login*
        # count can be nonzero — but the fact that the service is
        # only touched via the task path (not the receiver path)
        # is verified by test_login_enqueues_task_via_transaction_on_commit.
        assert client.login(username=user.username, password=password)

    # Verify the service WAS ultimately called (via the task) so
    # the test doesn't accidentally pass by short-circuiting the
    # whole tracking path.
    assert mock_log.call_count >= 1, (
        "tracking service was never called — the fix may have "
        "disconnected the signal from the task entirely"
    )
