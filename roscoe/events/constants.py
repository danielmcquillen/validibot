# roscoe/events/constants.py
from django.db import models
from django.utils.translation import gettext_lazy as _


class EventType(models.TextChoices):
    # CRUD operations
    SUBMISSION_CREATED = "submission.created", _("Submission Created")

    WORKFLOW_CREATED = "workflow.created", _("Workflow Created")
    WORKFLOW_UPDATED = "workflow.updated", _("Workflow Updated")
    WORKFLOW_DELETED = "workflow.deleted", _("Workflow Deleted")

    # Run lifecycle
    RUN_CREATED = "run.created", _("Run Created")
    RUN_STARTED = "run.started", _("Run Started")
    RUN_SUCCEEDED = "run.succeeded", _("Run Succeeded")
    RUN_FAILED = "run.failed", _("Run Failed")
    RUN_CANCELED = "run.canceled", _("Run Canceled")
    RUN_TIMED_OUT = "run.timed_out", _("Run Timed Out")

    # Step lifecycle
    RUN_STEP_STARTED = "runstep.started", _("Run Step Started")
    RUN_STEP_PASSED = "runstep.passed", _("Run Step Passed")
    RUN_STEP_FAILED = "runstep.failed", _("Run Step Failed")
    RUN_STEP_SKIPPED = "runstep.skipped", _("Run Step Skipped")

    # Ruleset / Validator (optional, if you plan to notify changes)
    RULESET_CREATED = "ruleset.created", _("Ruleset Created")
    RULESET_UPDATED = "ruleset.updated", _("Ruleset Updated")
    VALIDATOR_CREATED = "validator.created", _("Validator Created")
    VALIDATOR_UPDATED = "validator.updated", _("Validator Updated")

    # GitHub-specific events (if using GitHub integration)
    CHECK_SUITE_REQUESTED = "check_suite.requested", _("GitHub Check Suite Requested")
    CHECK_SUITE_COMPLETED = "check_suite.completed", _("GitHub Check Suite Completed")


# Convenient subsets
RUN_EVENTS = (
    EventType.RUN_CREATED,
    EventType.RUN_STARTED,
    EventType.RUN_SUCCEEDED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELED,
    EventType.RUN_TIMED_OUT,
)

RUN_STEP_EVENTS = (
    EventType.RUN_STEP_STARTED,
    EventType.RUN_STEP_PASSED,
    EventType.RUN_STEP_FAILED,
    EventType.RUN_STEP_SKIPPED,
)

CONFIG_EVENTS = (
    EventType.WORKFLOW_CREATED,
    EventType.WORKFLOW_UPDATED,
    EventType.WORKFLOW_DELETED,
    EventType.SUBMISSION_CREATED,
    EventType.RULESET_CREATED,
    EventType.RULESET_UPDATED,
    EventType.VALIDATOR_CREATED,
    EventType.VALIDATOR_UPDATED,
)

ALL_EVENTS = tuple(e.value for e in EventType)  # list[str] for quick membership tests
