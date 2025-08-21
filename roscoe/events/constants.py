# roscoe/events/constants.py
from django.db import models
from django.utils.translation import gettext_lazy as _


class EventType(models.TextChoices):
    # Run lifecycle
    RUN_CREATED = "run.created", _("Run Created")
    RUN_STARTED = "run.started", _("Run Started")
    RUN_SUCCEEDED = "run.succeeded", _("Run Succeeded")
    RUN_FAILED = "run.failed", _("Run Failed")
    RUN_CANCELED = "run.canceled", _("Run Canceled")
    RUN_TIMED_OUT = "run.timed_out", _("Run Timed Out")

    # Step lifecycle
    STEP_STARTED = "step.started", _("Step Started")
    STEP_PASSED = "step.passed", _("Step Passed")
    STEP_FAILED = "step.failed", _("Step Failed")
    STEP_SKIPPED = "step.skipped", _("Step Skipped")

    # Ruleset / Validator (optional, if you plan to notify changes)
    RULESET_CREATED = "ruleset.created", _("Ruleset Created")
    RULESET_UPDATED = "ruleset.updated", _("Ruleset Updated")
    VALIDATOR_CREATED = "validator.created", _("Validator Created")
    VALIDATOR_UPDATED = "validator.updated", _("Validator Updated")


# Convenient subsets
RUN_EVENTS = (
    EventType.RUN_CREATED,
    EventType.RUN_STARTED,
    EventType.RUN_SUCCEEDED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELED,
    EventType.RUN_TIMED_OUT,
)

STEP_EVENTS = (
    EventType.STEP_STARTED,
    EventType.STEP_PASSED,
    EventType.STEP_FAILED,
    EventType.STEP_SKIPPED,
)

CONFIG_EVENTS = (
    EventType.RULESET_CREATED,
    EventType.RULESET_UPDATED,
    EventType.VALIDATOR_CREATED,
    EventType.VALIDATOR_UPDATED,
)

ALL_EVENTS = tuple(e.value for e in EventType)  # list[str] for quick membership tests
