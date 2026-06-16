"""
Login / logout tracking signal receivers.

Extracts context (org, user-agent, path, derived channel) from the
request on the signal thread, then hands the event off to a
:class:`validibot.tracking.dispatch.TrackingDispatcher` — which picks
the right async backend for the current ``DEPLOYMENT_TARGET``
(inline for tests, Celery for Docker Compose, Cloud Tasks for GCP).

Why asynchronous:

* The signal fires on the auth-path critical section. An inline
  tracking insert adds DB round-trip latency to every login /
  logout, on top of whatever slower storage (separate replica,
  WAL-backed disk) tracking eventually moves to.
* ``transaction.on_commit`` guarantees the dispatch only happens
  when the surrounding transaction (if any) commits — an auth
  flow that rolls back will not produce a ghost login event.

Why routed through a dispatcher:

* GCP deployments have no Redis broker. The previous direct
  ``log_tracking_event_task.delay()`` call broke 2FA login in prod
  with a 500 when Celery tried to reach ``redis://localhost:6379``.
  The dispatcher selects Cloud Tasks on GCP, Celery on Docker
  Compose, and inline execution in tests — each deployment uses the
  async mechanism it actually has.
* Adding a new backend (AWS SQS, Pub/Sub, etc.) is a subclass +
  registry entry — the signal receivers stay unchanged.
"""

from __future__ import annotations

import logging

from allauth.account.signals import email_confirmed
from allauth.account.signals import user_signed_up
from django.contrib.auth.signals import user_logged_in
from django.contrib.auth.signals import user_logged_out
from django.db import transaction
from django.dispatch import receiver

from validibot.events.constants import AppEventType
from validibot.tracking.constants import TrackingEventType

logger = logging.getLogger(__name__)


def _extract_org(user):
    if user is None:
        return None
    if hasattr(user, "get_current_org"):
        try:
            return user.get_current_org()
        except Exception:  # pragma: no cover - defensive
            return None
    return None


def _build_request_metadata(request) -> dict[str, str]:
    if not request:
        return {}
    meta: dict[str, str] = {}
    user_agent = request.headers.get("user-agent")
    if user_agent:
        meta["user_agent"] = user_agent
    path = getattr(request, "path", None)
    if path:
        meta["path"] = path
    return meta


def _derive_channel(metadata: dict[str, str]) -> str:
    return "api" if metadata.get("path", "").startswith("/api") else "web"


def _enqueue_tracking_event(
    *,
    app_event_type: AppEventType,
    user_id: int | None,
    org_id: int | None,
    extra_data: dict[str, str],
    channel: str,
) -> None:
    """Hand a tracking event off to the configured dispatcher on commit.

    Scheduling the dispatch inside ``transaction.on_commit`` is the
    piece that stays here rather than moving into the dispatcher
    hierarchy: every dispatcher benefits from not firing for a
    rolled-back transaction, but the hook is Django-transaction-
    specific and lives in the caller's DB context. Dispatchers stay
    transport-only; callers stay transaction-aware.

    The inner try/except is a last-resort safety net. The dispatcher
    contract promises ``dispatch()`` won't raise for transient
    failures (broker down, API error, missing config — all return a
    response with ``error`` set). But a genuine programming error
    (bad import, attribute error) could still leak out, and the auth
    request must not 500 because of a tracking bug. Broad catch is
    correct here; ``exc_info=True`` preserves the traceback in Cloud
    Logging so the root cause remains diagnosable.
    """
    # Local imports keep signals.py importable during Django's
    # ``check`` / ``makemigrations`` phases — before apps are fully
    # loaded and before a dispatcher could safely be instantiated.
    from validibot.tracking.dispatch import TrackingEventRequest
    from validibot.tracking.dispatch import get_tracking_dispatcher

    event_request = TrackingEventRequest(
        event_type=TrackingEventType.APP_EVENT,
        app_event_type=str(app_event_type),
        user_id=user_id,
        org_id=org_id,
        extra_data=extra_data or None,
        channel=channel,
    )

    def _send() -> None:
        try:
            dispatcher = get_tracking_dispatcher()
            response = dispatcher.dispatch(event_request)
            if response.error:
                # The dispatcher already logged with its own context;
                # logging again here would double up. We only care
                # here whether an unexpected exception escapes — the
                # ``error`` field means the dispatcher handled it
                # cleanly.
                return
        except Exception:
            logger.warning(
                "Failed to dispatch tracking event (dropped): "
                "app_event_type=%s user_id=%s channel=%s",
                app_event_type,
                user_id,
                channel,
                exc_info=True,
            )

    transaction.on_commit(_send)


@receiver(user_logged_in)
def log_user_logged_in(sender, request, user, **kwargs):
    """Dispatch a USER_LOGGED_IN tracking event.

    All request-bound data (headers, path, current org) is captured
    here synchronously because ``request`` won't survive past the
    response boundary. Only primitives cross into the dispatcher.
    """
    org = _extract_org(user)
    metadata = _build_request_metadata(request)
    channel = _derive_channel(metadata)
    extra_data = {k: v for k, v in metadata.items() if v}

    _enqueue_tracking_event(
        app_event_type=AppEventType.USER_LOGGED_IN,
        user_id=getattr(user, "pk", None),
        org_id=getattr(org, "pk", None) if org else None,
        extra_data=extra_data,
        channel=channel,
    )


@receiver(user_logged_out)
def log_user_logged_out(sender, request, user, **kwargs):
    """Dispatch a USER_LOGGED_OUT tracking event.

    ``user`` may be ``None`` when the logout request had no
    authenticated user attached (rare but valid). The event still
    records with ``user_id=None``; tracking analytics treats
    anonymous logouts as "session ended without attribution."
    """
    org = _extract_org(user)
    metadata = _build_request_metadata(request)
    channel = _derive_channel(metadata)
    extra_data = {k: v for k, v in metadata.items() if v}

    _enqueue_tracking_event(
        app_event_type=AppEventType.USER_LOGGED_OUT,
        user_id=getattr(user, "pk", None) if user else None,
        org_id=getattr(org, "pk", None) if org else None,
        extra_data=extra_data,
        channel=channel,
    )


# ── activation funnel (signup / email verification) ──────────────────


def _enqueue_auth_funnel_event(app_event_type, user, request) -> None:
    """Shared body for the signup / email-verified funnel receivers.

    Same async, on-commit dispatch as login/logout: capture request
    primitives synchronously, hand off to the dispatcher on commit.
    """
    org = _extract_org(user)
    metadata = _build_request_metadata(request)
    channel = _derive_channel(metadata)
    _enqueue_tracking_event(
        app_event_type=app_event_type,
        user_id=getattr(user, "pk", None),
        org_id=getattr(org, "pk", None) if org else None,
        extra_data={k: v for k, v in metadata.items() if v},
        channel=channel,
    )


@receiver(user_signed_up)
def log_user_signed_up(sender, request, user, **kwargs):
    """Dispatch a USER_REGISTERED event — the top of the activation funnel.

    Fired by allauth right after account creation (before email
    verification, which lands its own USER_EMAIL_VERIFIED event).
    """
    _enqueue_auth_funnel_event(AppEventType.USER_REGISTERED, user, request)


@receiver(email_confirmed)
def log_email_confirmed(sender, request, email_address, **kwargs):
    """Dispatch a USER_EMAIL_VERIFIED event — an activation checkpoint.

    ``email_confirmed`` carries no ``user`` argument, so the owner is
    resolved from ``email_address.user``. The address value itself is
    never recorded — analytics is org / user-keyed, not PII.
    """
    user = getattr(email_address, "user", None)
    _enqueue_auth_funnel_event(AppEventType.USER_EMAIL_VERIFIED, user, request)


# ── model-lifecycle tracking (submission + config analytics) ─────────


def _track_model_event(app_event_type, instance) -> None:
    """Record a model-lifecycle tracking event via the direct service.

    Resolves project / org / user defensively from the instance —
    analytics cares most about the org dimension; project and user are
    best-effort (``getattr`` so a model without one records ``None``).

    Uses the synchronous service (not the async dispatcher) for
    consistency with the validation-run tracking, and because these fire
    inside the same transaction as the model change: a rolled-back save
    rolls back the event too (no ghost event), and
    ``TrackingEventService`` swallows errors so a tracking failure never
    breaks the write.
    """
    from validibot.tracking.services import TrackingEventService

    TrackingEventService().log_tracking_event(
        event_type=TrackingEventType.APP_EVENT,
        app_event_type=app_event_type,
        project=getattr(instance, "project", None),
        org=getattr(instance, "org", None),
        user=getattr(instance, "user", None),
    )


def on_submission_created(sender, instance, created, **kwargs) -> None:
    """SUBMISSION_CREATED — data submitted, a core funnel action."""
    if not created:
        return
    _track_model_event(AppEventType.SUBMISSION_CREATED, instance)


def on_workflow_created(sender, instance, created, **kwargs) -> None:
    """WORKFLOW_CREATED — an activation milestone.

    Only creation is tracked, not every save: a workflow is saved often
    for internal reasons (counters, status) that would drown the
    analytics signal. Meaningful config *changes* are covered by the
    audit log instead (WORKFLOW_UPDATED there carries the field diff).
    """
    if not created:
        return
    _track_model_event(AppEventType.WORKFLOW_CREATED, instance)


def on_workflow_deleted(sender, instance, **kwargs) -> None:
    """WORKFLOW_DELETED — a feature-abandonment signal."""
    _track_model_event(AppEventType.WORKFLOW_DELETED, instance)


def on_ruleset_created(sender, instance, created, **kwargs) -> None:
    """RULESET_CREATED — an author configured a rule set."""
    if not created:
        return
    _track_model_event(AppEventType.RULESET_CREATED, instance)


def on_validator_created(sender, instance, created, **kwargs) -> None:
    """VALIDATOR_CREATED — an author added a custom validator."""
    if not created:
        return
    _track_model_event(AppEventType.VALIDATOR_CREATED, instance)


def connect_model_tracking_receivers() -> None:
    """Connect the model-lifecycle tracking receivers.

    Wired explicitly here (rather than ``@receiver`` at module import)
    so other apps' models aren't imported until the registry is ready —
    mirrors the audit app's wiring. The auth-signal receivers above stay
    on ``@receiver`` because they import no models.
    """
    from django.db.models.signals import post_delete
    from django.db.models.signals import post_save

    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator
    from validibot.workflows.models import Workflow

    post_save.connect(
        on_submission_created,
        sender=Submission,
        dispatch_uid="validibot_tracking.on_submission_created",
    )
    post_save.connect(
        on_workflow_created,
        sender=Workflow,
        dispatch_uid="validibot_tracking.on_workflow_created",
    )
    post_delete.connect(
        on_workflow_deleted,
        sender=Workflow,
        dispatch_uid="validibot_tracking.on_workflow_deleted",
    )
    post_save.connect(
        on_ruleset_created,
        sender=Ruleset,
        dispatch_uid="validibot_tracking.on_ruleset_created",
    )
    post_save.connect(
        on_validator_created,
        sender=Validator,
        dispatch_uid="validibot_tracking.on_validator_created",
    )
