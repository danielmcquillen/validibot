"""Product-analytics event vocabulary — Pillar 2 (``TrackingEvent``).

These are the *tracking / analytics* event types, recorded via
``validibot.tracking.services.TrackingEventService`` into the
``tracking_trackingevent`` table. They answer "what are users doing, in
aggregate?" — best-effort, lossy-tolerant, ~90-day retention.

Naming convention — **dotted ``object.verb``** (``workflow.created``,
``user.logged_in``). This is deliberately *different* from the audit
log's flat ``snake_case`` values. The shape of a value tells you which
system it belongs to:

* ``"workflow.created"`` (dotted) → analytics — this module.
* ``"workflow_created"`` (flat)   → audit — ``validibot.audit.constants.AuditAction``.

Do not confuse the two. Several member *names* exist in both enums
(``WORKFLOW_CREATED``, ``RULESET_CREATED`` …) because the same real-world
event is both analytics-worthy and audit-worthy — but they are distinct
vocabularies with distinct values, retention, and consumers. When you
import ``WORKFLOW_CREATED``, make sure you grabbed it from the module for
the system you are writing to.

See AGENTS.md → "Observability — which log do I write to?" for the
routing rule, and ``validibot-project/docs/observability/`` for the deep
dive (team-internal).
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class AppEventType(models.TextChoices):
    """Fine-grained analytics event types (Pillar 2 — ``TrackingEvent``).

    Dotted ``object.verb`` values. The audit-log counterpart is
    ``validibot.audit.constants.AuditAction`` (flat ``snake_case``); keep
    the two straight — see the module docstring.
    """

    # CRUD operations
    SUBMISSION_CREATED = "submission.created", _("Submission Created")

    WORKFLOW_CREATED = "workflow.created", _("Workflow Created")
    WORKFLOW_UPDATED = "workflow.updated", _("Workflow Updated")
    WORKFLOW_DELETED = "workflow.deleted", _("Workflow Deleted")

    # Run lifecycle
    VALIDATION_RUN_CREATED = "validation_run.created", _("Run Created")
    VALIDATION_RUN_STARTED = "validation_run.started", _("Run Started")
    VALIDATION_RUN_SUCCEEDED = "validation_run.succeeded", _("Run Succeeded")
    VALIDATION_RUN_FAILED = "validation_run.failed", _("Run Failed")
    VALIDATION_RUN_CANCELED = "validation_run.canceled", _("Run Canceled")
    VALIDATION_RUN_TIMED_OUT = "validation_run.timed_out", _("Run Timed Out")

    # Step lifecycle
    VALIDATION_RUN_STEP_STARTED = "validation_run_step.started", _("Run Step Started")
    VALIDATION_RUN_STEP_PASSED = "validation_run_step.passed", _("Run Step Passed")
    VALIDATION_RUN_STEP_FAILED = "validation_run_step.failed", _("Run Step Failed")
    VALIDATION_RUN_STEP_SKIPPED = "validation_run_step.skipped", _("Run Step Skipped")

    # Ruleset / Validator (optional, if you plan to notify changes)
    RULESET_CREATED = "ruleset.created", _("Ruleset Created")
    RULESET_UPDATED = "ruleset.updated", _("Ruleset Updated")
    VALIDATOR_CREATED = "validator.created", _("Validator Created")
    VALIDATOR_UPDATED = "validator.updated", _("Validator Updated")

    # GitHub-specific events (if using GitHub integration)
    CHECK_SUITE_REQUESTED = "check_suite.requested", _("GitHub Check Suite Requested")
    CHECK_SUITE_COMPLETED = "check_suite.completed", _("GitHub Check Suite Completed")

    # User lifecycle
    USER_REGISTERED = "user.registered", _("User Registered")
    USER_EMAIL_VERIFIED = "user.email_verified", _("User Email Verified")
    USER_LOGGED_IN = "user.logged_in", _("User Logged In")
    USER_LOGGED_OUT = "user.logged_out", _("User Logged Out")

    # Invitations
    INVITE_CREATED = "invite.created", _("Invite Created")
    INVITE_ACCEPTED = "invite.accepted", _("Invite Accepted")
    INVITE_DECLINED = "invite.declined", _("Invite Declined")


# Convenient subsets
RUN_EVENTS = (
    AppEventType.VALIDATION_RUN_CREATED,
    AppEventType.VALIDATION_RUN_STARTED,
    AppEventType.VALIDATION_RUN_SUCCEEDED,
    AppEventType.VALIDATION_RUN_FAILED,
    AppEventType.VALIDATION_RUN_CANCELED,
    AppEventType.VALIDATION_RUN_TIMED_OUT,
)

RUN_STEP_EVENTS = (
    AppEventType.VALIDATION_RUN_STEP_STARTED,
    AppEventType.VALIDATION_RUN_STEP_PASSED,
    AppEventType.VALIDATION_RUN_STEP_FAILED,
    AppEventType.VALIDATION_RUN_STEP_SKIPPED,
)

CONFIG_EVENTS = (
    AppEventType.WORKFLOW_CREATED,
    AppEventType.WORKFLOW_UPDATED,
    AppEventType.WORKFLOW_DELETED,
    AppEventType.SUBMISSION_CREATED,
    AppEventType.RULESET_CREATED,
    AppEventType.RULESET_UPDATED,
    AppEventType.VALIDATOR_CREATED,
    AppEventType.VALIDATOR_UPDATED,
)

ALL_EVENTS = tuple(
    e.value for e in AppEventType
)  # list[str] for quick membership tests
