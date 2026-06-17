"""HTTP-safety tests for ``WorkflowStepWizardView._ensure_validator_defaults``.

The step wizard backfills missing FMU validator defaults (FMU's accepted
file types/data formats expanded to include JSON/TEXT after some validators
were created). That backfill writes to the database — and a write must never
happen on an HTTP ``GET``: GET is defined to be safe/side-effect-free, and
crawlers, link-preview bots, and browser prefetch all issue GETs. Persistence
is therefore gated to mutating ``POST`` requests.

Regression for ADR 04-23 §bug.validator_defaults_on_get.
"""

from __future__ import annotations

import pytest
from django.test import RequestFactory

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ValidationType
from validibot.validations.models import Validator
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.views.steps import WorkflowStepWizardView

pytestmark = pytest.mark.django_db


def _fmu_validator_missing_defaults() -> Validator:
    """Return an FMU validator whose supported lists omit JSON/TEXT.

    We force the supported-type lists to empty (bypassing the factory's
    default-populating lambdas) to recreate the pre-expansion state that the
    wizard backfill exists to repair. Empty lists — rather than ``None`` —
    keep the test independent of the column's nullability while still making
    ``_ensure_validator_defaults`` detect a needed change.
    """
    # A system FMU validator (is_system=True, no org) is exempt from the
    # "assign an FMU asset" model constraint that Validator.save()'s
    # full_clean() enforces. Crucially we must NOT attach an org: save()
    # forces is_system=False whenever org_id is set (models.py), which would
    # re-trigger that constraint on the POST re-save. The wizard backfill only
    # keys on validation_type == FMU, so a system validator exercises the same
    # code path without needing an fmu_model fixture.
    validator = ValidatorFactory(
        validation_type=ValidationType.FMU,
        is_system=True,
    )
    Validator.objects.filter(pk=validator.pk).update(
        supported_file_types=[],
        supported_data_formats=[],
    )
    validator.refresh_from_db()
    return validator


def _wizard_view_for(method: str) -> WorkflowStepWizardView:
    """A wizard view instance whose request uses the given HTTP method."""
    view = WorkflowStepWizardView()
    view.request = getattr(RequestFactory(), method)("/")
    return view


class TestEnsureValidatorDefaultsHttpSafety:
    """GET must not persist the backfill; POST may."""

    def test_get_does_not_persist_backfill(self):
        """On a GET, the backfill stays in memory — the DB row is untouched.

        This is the core regression: rendering the wizard (or any crawler /
        prefetch GET) must not mutate state. The in-memory list may be updated
        for display, but nothing is written, so a reload from the database
        still shows the original empty lists.
        """
        validator = _fmu_validator_missing_defaults()
        view = _wizard_view_for("get")

        view._ensure_validator_defaults(validator)

        validator.refresh_from_db()
        assert validator.supported_file_types == []
        assert validator.supported_data_formats == []

    def test_post_persists_backfill(self):
        """On a POST, the backfill is written so the validator gains JSON/TEXT.

        The fix only moves the write off the GET path — it must still happen on
        a genuine mutating request, otherwise the validator would never get its
        expanded defaults.
        """
        validator = _fmu_validator_missing_defaults()
        view = _wizard_view_for("post")

        view._ensure_validator_defaults(validator)

        validator.refresh_from_db()
        assert SubmissionFileType.JSON in validator.supported_file_types
        assert SubmissionFileType.TEXT in validator.supported_file_types
