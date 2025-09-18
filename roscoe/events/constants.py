# roscoe/events/constants.py
from django.db import models
from django.utils.translation import gettext_lazy as _


class AppEventType(models.TextChoices):
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
