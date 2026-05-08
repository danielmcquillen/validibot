"""Create the ``Basic Users`` and ``Guests`` classifier groups + backfill.

The two groups are classification metadata — they carry no permissions
of their own; they exist so the codebase can answer "is this account a
guest or a regular user?" with one stored fact instead of a derived
predicate.

Backfill predicate ("has active workflow grants AND no active
memberships" → ``Guests``; everyone else → ``Basic Users``) is the
intuitive read of "this user only operates as a guest at upgrade time."
The classification can later be changed only by ``classify_as_guest`` /
``classify_as_basic`` (or the audited ``promote_user`` command).
"""

from __future__ import annotations

from django.db import migrations

# Hard-coded here on purpose: migrations must not import live constants
# because constants files evolve while migrations remain pinned to the
# state they describe. Keeping the names inline guarantees this migration
# replays identically against any future tree.
GROUP_BASIC = "Basic Users"
GROUP_GUEST = "Guests"


def create_user_kind_groups(apps, schema_editor):
    """Create the two classifier groups and backfill all existing users.

    Idempotent: ``get_or_create`` on the groups, ``add()`` on user-group
    links is a no-op if the link already exists. Safe to re-run if a
    previous run failed partway through.
    """

    Group = apps.get_model("auth", "Group")
    User = apps.get_model("users", "User")
    WorkflowAccessGrant = apps.get_model("workflows", "WorkflowAccessGrant")

    basic_group, _ = Group.objects.get_or_create(name=GROUP_BASIC)
    guest_group, _ = Group.objects.get_or_create(name=GROUP_GUEST)

    guest_user_ids = set(
        WorkflowAccessGrant.objects.filter(
            is_active=True,
        )
        .exclude(
            user__memberships__is_active=True,
        )
        .values_list("user_id", flat=True),
    )

    for user in User.objects.iterator():
        target_group = guest_group if user.id in guest_user_ids else basic_group
        # ``Group.user_set.add`` is the historical-model equivalent of
        # ``user.groups.add(group)`` — historical ``User`` doesn't expose
        # the reverse descriptor, so we go through the group side.
        target_group.user_set.add(user)


def remove_user_kind_groups(apps, schema_editor):
    """Reverse: remove the two classifier groups (memberships cascade)."""

    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=[GROUP_BASIC, GROUP_GUEST]).delete()


class Migration(migrations.Migration):
    """Backfill is one-shot. Forward = create + classify; reverse = drop."""

    dependencies = [
        ("users", "0003_wipe_pre_encryption_authenticators"),
        # The backfill query reads ``WorkflowAccessGrant`` rows, so we
        # depend on whichever workflows migration last touched its shape.
        # Pinning here means a future rename of ``user_id`` or
        # ``is_active`` would have to rewrite this dependency too,
        # surfacing the breakage at migrate-time rather than as silent
        # data corruption.
        ("workflows", "0018_add_workflow_publish_invariants"),
    ]

    operations = [
        migrations.RunPython(
            create_user_kind_groups,
            remove_user_kind_groups,
        ),
    ]
