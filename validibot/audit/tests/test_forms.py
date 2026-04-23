"""Unit tests for ``AuditLogFilterForm``.

The form is the single source of truth for filter semantics shared
between the list view and the export endpoint. Tests here exercise
it in isolation so a regression shows up as a form-level failure
rather than a confusing UI or export bug downstream.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from validibot.audit.constants import AuditAction
from validibot.audit.forms import AuditLogFilterForm
from validibot.audit.models import AuditActor
from validibot.audit.models import AuditLogEntry
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory


def _make_entry(
    *,
    org,
    action: AuditAction = AuditAction.WORKFLOW_UPDATED,
    actor_email: str = "actor@example.com",
    target_type: str = "workflows.Workflow",
    occurred_offset: timedelta = timedelta(),
) -> AuditLogEntry:
    """Build an entry with controllable ``occurred_at`` for ordering."""

    actor = AuditActor.objects.create(email=actor_email)
    entry = AuditLogEntry.objects.create(
        actor=actor,
        org=org,
        action=action.value,
        target_type=target_type,
        target_id="1",
    )
    if occurred_offset:
        AuditLogEntry.objects.filter(pk=entry.pk).update(
            occurred_at=timezone.now() + occurred_offset,
        )
        entry.refresh_from_db()
    return entry


class AuditLogFilterFormValidationTests(TestCase):
    """Empty / full / malformed form validation paths."""

    def test_empty_form_is_valid(self) -> None:
        """An empty form is the "no filters" landing state. It must
        validate so the list view can render unfiltered results.
        """

        form = AuditLogFilterForm({})
        self.assertTrue(form.is_valid())

    def test_unbound_form_does_not_crash(self) -> None:
        """An unbound form (e.g. first render before GET arrives) is
        still a legitimate state — ``apply_to_queryset`` must return
        the queryset unchanged rather than raising.
        """

        form = AuditLogFilterForm()
        queryset = AuditLogEntry.objects.none()
        self.assertIs(form.apply_to_queryset(queryset), queryset)

    def test_date_range_order_is_enforced(self) -> None:
        """A mis-ordered range (from > to) must produce a form error
        rather than silently returning zero rows — the operator
        needs to see *why* their filter returned nothing.
        """

        form = AuditLogFilterForm(
            {
                "date_from": "2026-02-01",
                "date_to": "2026-01-01",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn(
            "Start date must not be after end date.",
            "\n".join(form.non_field_errors()),
        )

    def test_unknown_action_is_rejected(self) -> None:
        """``action`` is a ChoiceField — a random string must be a
        validation error, not a silent "no results" outcome.
        """

        form = AuditLogFilterForm({"action": "definitely_not_a_real_action"})
        self.assertFalse(form.is_valid())
        self.assertIn("action", form.errors)


class AuditLogFilterFormPredicateTests(TestCase):
    """Each filter narrows the queryset correctly."""

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    def test_action_filter_narrows_to_matching_rows(self) -> None:
        """Only entries whose ``action`` matches should survive."""

        _make_entry(org=self.org, action=AuditAction.WORKFLOW_UPDATED)
        _make_entry(org=self.org, action=AuditAction.LOGIN_SUCCEEDED)

        form = AuditLogFilterForm(
            {"action": AuditAction.LOGIN_SUCCEEDED.value},
        )
        self.assertTrue(form.is_valid())

        result = form.apply_to_queryset(AuditLogEntry.objects.all())
        actions = list(result.values_list("action", flat=True))
        self.assertEqual(actions, [AuditAction.LOGIN_SUCCEEDED.value])

    def test_actor_filter_matches_captured_email(self) -> None:
        """The captured ``actor.email`` (denormalised at write time)
        must match — that's the field that survives user deletion.
        """

        _make_entry(org=self.org, actor_email="alice@example.com")
        _make_entry(org=self.org, actor_email="bob@example.com")

        form = AuditLogFilterForm({"actor": "alice"})
        self.assertTrue(form.is_valid())

        result = form.apply_to_queryset(AuditLogEntry.objects.all())
        self.assertEqual(result.count(), 1)

    def test_actor_filter_matches_user_record_email(self) -> None:
        """If the captured ``actor.email`` is empty but the actor has
        a ``user`` FK, the form must also match against the live
        ``actor.user.email`` — otherwise searching by a current
        member's email would silently return nothing when the
        capture was sparse.
        """

        user = UserFactory(email="carol@example.com")
        actor = AuditActor.objects.create(user=user, email=None)
        AuditLogEntry.objects.create(
            actor=actor,
            org=self.org,
            action=AuditAction.WORKFLOW_UPDATED.value,
            target_type="workflows.Workflow",
            target_id="1",
        )

        form = AuditLogFilterForm({"actor": "carol"})
        self.assertTrue(form.is_valid())

        result = form.apply_to_queryset(AuditLogEntry.objects.all())
        self.assertEqual(result.count(), 1)

    def test_target_type_is_exact_match(self) -> None:
        """Target type uses ``==`` so a search for ``workflows.Workflow``
        doesn't spuriously match ``workflows.WorkflowStep``.
        """

        _make_entry(org=self.org, target_type="workflows.Workflow")
        _make_entry(org=self.org, target_type="validations.Ruleset")

        form = AuditLogFilterForm({"target_type": "workflows.Workflow"})
        self.assertTrue(form.is_valid())
        result = form.apply_to_queryset(AuditLogEntry.objects.all())
        self.assertEqual(result.count(), 1)

    def test_date_range_bounds_span_full_days(self) -> None:
        """A range of ``[2026-03-05, 2026-03-05]`` should include
        entries that occurred anywhere within that single day, not
        only those at exactly 00:00:00. That's the whole point of
        the clean_date_range logic anchoring to start/end-of-day.
        """

        # Use explicit timestamps on known days to avoid timezone
        # brittleness — ``date.today()`` + ``timedelta(hours=-2)`` can
        # straddle midnight in the server's timezone depending on when
        # the test runs, producing intermittent failures.
        import datetime as _dt

        tz = timezone.get_current_timezone()
        target_day = _dt.date(2026, 3, 15)
        day_before = _dt.date(2026, 3, 14)

        # Noon of the target day is inside [start-of-day, end-of-day]
        # for any timezone, so this entry always lands inside the
        # filter window.
        in_range = _make_entry(org=self.org, actor_email="in@example.com")
        AuditLogEntry.objects.filter(pk=in_range.pk).update(
            occurred_at=timezone.make_aware(
                _dt.datetime.combine(target_day, _dt.time(12, 0)),
                tz,
            ),
        )
        # 23:00 of the previous day — outside the window regardless of
        # timezone rounding.
        out_of_range = _make_entry(org=self.org, actor_email="out@example.com")
        AuditLogEntry.objects.filter(pk=out_of_range.pk).update(
            occurred_at=timezone.make_aware(
                _dt.datetime.combine(day_before, _dt.time(23, 0)),
                tz,
            ),
        )

        form = AuditLogFilterForm(
            {
                "date_from": target_day.isoformat(),
                "date_to": target_day.isoformat(),
            },
        )
        self.assertTrue(form.is_valid())
        result = form.apply_to_queryset(AuditLogEntry.objects.all())
        emails = set(result.values_list("actor__email", flat=True))
        self.assertEqual(emails, {"in@example.com"})

    def test_invalid_form_returns_none_queryset(self) -> None:
        """When validation fails, ``apply_to_queryset`` must return an
        empty queryset — better UX than the "unfiltered" default,
        which would silently show everything despite the broken
        filter input.
        """

        _make_entry(org=self.org)

        form = AuditLogFilterForm({"action": "bogus"})
        self.assertFalse(form.is_valid())
        result = form.apply_to_queryset(AuditLogEntry.objects.all())
        self.assertFalse(result.exists())
