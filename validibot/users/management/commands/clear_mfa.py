"""Delete MFA authenticators for a user (or all users) — non-interactive.

Designed for the break-glass scenario where ``DJANGO_MFA_ENCRYPTION_KEY``
has been rotated or lost and affected users can no longer complete the
MFA challenge: allauth tries to ``decrypt()`` the stored TOTP secret,
:class:`~validibot.users.mfa_adapter.ValidibotMFAAdapter` raises
:class:`~cryptography.fernet.InvalidToken`, and the login flow 500s at
``/accounts/2fa/authenticate/``.

Removing the affected ``Authenticator`` rows lets allauth skip the MFA
stage on next login so the user can re-enroll under the new key from the
Security page. This is the recovery path referenced in
:file:`docs/dev_docs/how-to/configure-mfa.md`.

Usage::

    # Clear one user by email (recommended for single-user lockouts)
    python manage.py clear_mfa --email daniel@example.com

    # Clear one user by primary key (useful if email itself is suspect)
    python manage.py clear_mfa --user-id 42

    # Mass clear after a key rotation that invalidates every user's TOTP
    python manage.py clear_mfa --all-users

    # Preview what would be deleted without touching the DB
    python manage.py clear_mfa --email daniel@example.com --dry-run

Invoke in production via ``just gcp management-cmd prod "clear_mfa --email ..."``.
"""

from __future__ import annotations

from allauth.mfa.models import Authenticator
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

User = get_user_model()


class Command(BaseCommand):
    """Delete ``mfa_authenticator`` rows so allauth skips the MFA stage on login."""

    help = (
        "Delete MFA authenticators for a user (or all users). "
        "Use after MFA_ENCRYPTION_KEY rotation to unstick locked-out users."
    )

    def add_arguments(self, parser):
        # Exactly one selector is required; argparse enforces this via the
        # mutually-exclusive group so we don't have to re-validate in handle().
        selector = parser.add_mutually_exclusive_group(required=True)
        selector.add_argument(
            "--email",
            type=str,
            help="Email address of the user whose authenticators should be cleared.",
        )
        selector.add_argument(
            "--user-id",
            type=int,
            help="Primary key of the user whose authenticators should be cleared.",
        )
        selector.add_argument(
            "--all-users",
            action="store_true",
            help=(
                "Clear authenticators for EVERY user. Use only after a "
                "confirmed mass key-rotation event."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without modifying the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        queryset = self._select_queryset(options)

        # Pre-compute the report before deleting so we can log what went away
        # even after the rows are gone. ``values_list`` avoids loading full
        # model instances for what is otherwise a throwaway summary.
        rows = list(queryset.values_list("id", "user_id", "type"))
        count = len(rows)

        if count == 0:
            self.stdout.write(
                self.style.WARNING(
                    "No MFA authenticators matched — nothing to delete.",
                ),
            )
            return

        if dry_run:
            self.stdout.write(
                self.style.NOTICE(
                    f"[dry-run] Would delete {count} authenticator(s):",
                ),
            )
            for row_id, user_id, auth_type in rows:
                self.stdout.write(
                    f"  id={row_id} user_id={user_id} type={auth_type}",
                )
            return

        queryset.delete()
        self.stdout.write(
            self.style.SUCCESS(f"Deleted {count} authenticator(s):"),
        )
        for row_id, user_id, auth_type in rows:
            self.stdout.write(f"  id={row_id} user_id={user_id} type={auth_type}")

    def _select_queryset(self, options):
        """Return the ``Authenticator`` queryset implied by the CLI selector.

        Split out so the mutually-exclusive group's branching doesn't clutter
        :meth:`handle`. Raises :class:`CommandError` with a user-friendly
        message when an ``--email``/``--user-id`` lookup misses — a missed
        lookup is far more likely to be a typo than an empty result, and
        treating it as such prevents accidental "I thought I cleared Daniel"
        silent successes.
        """
        if options["all_users"]:
            return Authenticator.objects.all()

        email = options["email"]
        if email:
            try:
                user = User.objects.get(email=email)
            except User.DoesNotExist as exc:
                raise CommandError(
                    f"No user found with email {email!r}. Check for typos or "
                    f"verify against account_emailaddress.email — allauth "
                    f"authenticates against that table, not users_user.email.",
                ) from exc
            return Authenticator.objects.filter(user=user)

        user_id = options["user_id"]
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist as exc:
            raise CommandError(f"No user found with id {user_id}.") from exc
        return Authenticator.objects.filter(user=user)
