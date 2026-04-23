"""Query-string filter form for the audit log list + export views.

Both ``AuditLogListView`` and ``AuditLogExportView`` use the same
form so that an operator who narrowed the list view to a subset can
hit "Export" and get exactly that subset in CSV / JSONL. Centralising
the filter predicate keeps those two views from drifting apart.

The form is intentionally GET-bound (query string, not POST body) so
filtered pages are linkable and bookmark-able. The form only validates
— the actual queryset narrowing happens in ``apply_to_queryset``
below, which takes the already-validated ``cleaned_data`` and returns
a filtered queryset.

Design notes:

* **Everything optional.** An empty form produces an unfiltered
  queryset — that's the "no filters" landing state for the list view.
* **Action is a ChoiceField** built from ``AuditAction.choices`` so
  we get a dropdown with human labels and strict validation. A
  random string lands as a form error rather than a mysterious empty
  result.
* **Actor filter** is a plain ``icontains`` on email for both
  ``actor.email`` (denormalised capture) and ``actor.user.email``
  (current user record). That covers both "search for someone whose
  account has since been deleted" (actor.email survives user
  deletion) and "search for a current member".
* **Date range** is cleaned so ``date_from`` is start-of-day and
  ``date_to`` is end-of-day in the server's timezone. Otherwise a
  user typing "today" to find recent entries would get nothing back
  — their filter would resolve to ``[00:00 today, 00:00 today]``.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time
from typing import TYPE_CHECKING

from django import forms
from django.db.models import Q
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from validibot.audit.constants import AuditAction

if TYPE_CHECKING:
    from validibot.audit.models import AuditLogEntry

# Choices tuple for the ``action`` field. Prepend an empty option so
# "any action" is the visible default in the dropdown. The field is
# ``required=False``, but ChoiceField still needs the empty option
# present in ``choices`` to render a blank default.
_ACTION_CHOICES: tuple[tuple[str, str], ...] = (
    ("", _("Any action")),
    *AuditAction.choices,
)


class AuditLogFilterForm(forms.Form):
    """GET-bound filter form shared by list + export views."""

    action = forms.ChoiceField(
        required=False,
        choices=_ACTION_CHOICES,
        label=_("Action"),
    )
    actor = forms.CharField(
        required=False,
        label=_("Actor email"),
        help_text=_(
            "Matches against both the captured actor email (preserved "
            "after user deletion) and the current user record.",
        ),
    )
    target_type = forms.CharField(
        required=False,
        label=_("Target type"),
        help_text=_(
            "e.g. ``workflows.Workflow``. Exact match.",
        ),
    )
    date_from = forms.DateField(
        required=False,
        label=_("From"),
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    date_to = forms.DateField(
        required=False,
        label=_("To"),
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def clean(self) -> dict:
        """Validate that ``date_from`` is not after ``date_to``.

        A silent mis-ordering (e.g. the user swapped the two inputs)
        would produce zero rows — which the operator might misread as
        "nothing happened in that window". We raise instead so the UI
        shows the actual problem.
        """

        cleaned = super().clean()
        date_from = cleaned.get("date_from")
        date_to = cleaned.get("date_to")
        if date_from and date_to and date_from > date_to:
            raise forms.ValidationError(
                _("Start date must not be after end date."),
            )
        return cleaned

    def apply_to_queryset(
        self,
        queryset: QuerySet[AuditLogEntry],
    ) -> QuerySet[AuditLogEntry]:
        """Apply every populated filter to ``queryset`` and return it.

        Safe to call even when the form hasn't been validated; the
        helper defers to ``cleaned_data`` when available and treats
        missing values as "no filter for this field". That keeps the
        export view's implementation identical to the list view —
        neither has to reason about partially-validated forms.
        """

        # ``cleaned_data`` is only populated after ``is_valid()``.
        # Without that guard, callers that forgot to validate would
        # get a ``None`` on the attribute access. Be forgiving: treat
        # an unvalidated form the same as an empty one.
        if not self.is_bound:
            return queryset
        if not self.is_valid():
            # Validation errors mean the request was malformed; we
            # don't guess at what the operator meant. Return an empty
            # queryset so the UI renders "no results" rather than the
            # full unfiltered list — which would be very confusing.
            return queryset.none()

        data = self.cleaned_data

        action = data.get("action")
        if action:
            queryset = queryset.filter(action=action)

        actor = (data.get("actor") or "").strip()
        if actor:
            queryset = queryset.filter(
                Q(actor__email__icontains=actor)
                | Q(actor__user__email__icontains=actor),
            )

        target_type = (data.get("target_type") or "").strip()
        if target_type:
            queryset = queryset.filter(target_type=target_type)

        date_from = data.get("date_from")
        if date_from:
            # Start-of-day in the server timezone. Without this the
            # filter would anchor at midnight UTC, producing an
            # off-by-one window for operators in any non-UTC zone.
            tz = timezone.get_current_timezone()
            start = timezone.make_aware(
                datetime.combine(date_from, time.min),
                tz,
            )
            queryset = queryset.filter(occurred_at__gte=start)

        date_to = data.get("date_to")
        if date_to:
            tz = timezone.get_current_timezone()
            end = timezone.make_aware(
                datetime.combine(date_to, time.max),
                tz,
            )
            queryset = queryset.filter(occurred_at__lte=end)

        return queryset
