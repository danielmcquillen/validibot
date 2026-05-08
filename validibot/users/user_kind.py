"""User-kind classification helpers.

The ``Basic Users`` and ``Guests`` Django Groups label every user with a
system-wide kind. Two roles for the helpers here:

* **Read** the classification with
  :attr:`~validibot.users.models.User.user_kind`, which returns the
  appropriate :class:`~validibot.users.constants.UserKindGroup` value.
  Compare against ``UserKindGroup.GUEST`` or ``UserKindGroup.BASIC``
  at the call site.
* **Change** the classification by calling :func:`classify_as_basic`
  or :func:`classify_as_guest` here. The canonical audited promotion
  path (with personal-org provisioning + audit log entry in one
  transaction) is the ``promote_user`` management command, which
  composes these helpers; reach for that when the change is operator-
  driven rather than part of an automated flow.

These helpers are deliberately small and side-effect-light: just group
membership flips. The audit trail is captured by the ``m2m_changed``
signal on ``User.groups``, so callers do not need to record events
themselves.

Important: the user kind is a SYSTEM-WIDE property of the account, not a
per-workflow concern. To answer "does this user have guest access to
*this specific workflow*?", use the per-workflow grant machinery
(:class:`~validibot.workflows.models.WorkflowAccessGrant` queries or the
``Workflow.can_view`` helper) — a BASIC user can hold cross-org workflow
grants without becoming a GUEST.
"""

from __future__ import annotations

from django.contrib.auth.models import Group

from validibot.users.constants import UserKindGroup


def classify_as_basic(user) -> None:
    """Move ``user`` into the ``Basic Users`` classifier group.

    Idempotent — safe to call when the user is already classified as
    basic. Removes the ``Guests`` membership if present so the
    "exactly one classifier" invariant holds.
    """

    basic, _ = Group.objects.get_or_create(name=UserKindGroup.BASIC.value)
    user.groups.remove(*Group.objects.filter(name=UserKindGroup.GUEST.value))
    user.groups.add(basic)


def classify_as_guest(user) -> None:
    """Move ``user`` into the ``Guests`` classifier group.

    Idempotent — safe to call when the user is already classified as a
    guest. Removes the ``Basic Users`` membership if present so the
    "exactly one classifier" invariant holds.
    """

    guest, _ = Group.objects.get_or_create(name=UserKindGroup.GUEST.value)
    user.groups.remove(*Group.objects.filter(name=UserKindGroup.BASIC.value))
    user.groups.add(guest)
