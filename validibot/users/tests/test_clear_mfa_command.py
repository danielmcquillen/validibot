"""Tests for the :mod:`validibot.users.management.commands.clear_mfa` command.

The ``clear_mfa`` command is a break-glass tool for the scenario described
in :file:`docs/dev_docs/how-to/configure-mfa.md`: the MFA encryption key
has been rotated (or lost) and affected users can no longer complete the
MFA challenge because their stored TOTP secret can't be decrypted under
the new key. The command deletes the offending ``Authenticator`` rows so
allauth's MFA stage is skipped on next login and the user can re-enroll.

What we cover here:

- Each selector flag (``--email``, ``--user-id``, ``--all-users``) scopes
  deletion correctly and doesn't touch unrelated users' authenticators.
  This is the core invariant — a "clear MFA for Alice" command that also
  clears Bob's MFA would be catastrophic.
- ``--dry-run`` reports what would happen without modifying the database.
  We don't want operators to discover mid-incident that dry-run is a no-op
  that silently went ahead and deleted rows anyway.
- Missing users raise ``CommandError`` (not "deleted 0 rows") so a typo in
  an email doesn't look like a successful clear. The hint about
  ``account_emailaddress.email`` vs ``users_user.email`` is preserved in
  the error message because that divergence has bitten us in production.
- Empty matches (user exists but has no MFA rows) are a warning, not an
  error — this is the common "user already has no MFA" case during mass
  rotations and shouldn't fail a batch run.
- The mutually-exclusive selector group enforces exactly one of
  ``--email``/``--user-id``/``--all-users``, since "no selector" is an
  easy way to accidentally delete every row in the table.

We deliberately don't exercise the full allauth TOTP activation flow to
create realistic ``Authenticator`` rows — we construct them directly,
the same way :file:`test_mfa_adapter.py` does, to keep these tests fast
and independent of allauth's internals.
"""

from __future__ import annotations

from io import StringIO

import pytest
from allauth.mfa.models import Authenticator
from django.core.management import call_command
from django.core.management.base import CommandError

from validibot.users.models import User

pytestmark = pytest.mark.django_db


def _make_user(email: str, username: str) -> User:
    """Create a user suitable for attaching MFA rows to.

    We don't care about the password or any allauth-specific state —
    ``Authenticator.user`` is a plain FK to ``users_user``, so a minimal
    user is all we need.
    """
    return User.objects.create_user(
        username=username,
        email=email,
        password="test-password-not-used",  # noqa: S106
    )


def _make_authenticator(user: User, auth_type: str = "totp") -> Authenticator:
    """Attach an ``Authenticator`` row to ``user``.

    We store a placeholder in ``data`` rather than a real encrypted TOTP
    secret. The command only cares about the row's existence, not its
    contents — it never calls ``decrypt()`` — so a stub value keeps the
    test independent of ``MFA_ENCRYPTION_KEY`` setup.
    """
    return Authenticator.objects.create(
        user=user,
        type=auth_type,
        data={"secret": "placeholder-not-decrypted"},
    )


# ── Selector: --email ──────────────────────────────────────────────────
# The email path is the operator's primary interface: it mirrors how the
# user identifies themselves ("I'm daniel@…") and works even when the
# operator doesn't know the numeric user_id.


class TestClearByEmail:
    """``--email`` scopes deletion to that one user."""

    def test_deletes_only_target_users_rows(self):
        """Alice's authenticator is deleted; Bob's survives untouched.

        This is the single most important invariant of the command —
        regressions here would be catastrophic during a real incident.
        """
        alice = _make_user("alice@example.com", "alice")
        bob = _make_user("bob@example.com", "bob")
        alice_auth = _make_authenticator(alice)
        bob_auth = _make_authenticator(bob)

        call_command("clear_mfa", "--email", "alice@example.com", stdout=StringIO())

        assert not Authenticator.objects.filter(pk=alice_auth.pk).exists()
        assert Authenticator.objects.filter(pk=bob_auth.pk).exists()

    def test_deletes_all_of_targets_rows_including_recovery_codes(self):
        """Users with multiple authenticator rows (TOTP + recovery codes)
        get all of them cleared, not just the first matched.

        Recovery codes are stored as a separate ``Authenticator`` row of
        type ``recovery_codes``. Leaving those behind would leave the user
        half-enrolled and still subject to the MFA challenge.
        """
        alice = _make_user("alice@example.com", "alice")
        _make_authenticator(alice, auth_type="totp")
        _make_authenticator(alice, auth_type="recovery_codes")

        call_command("clear_mfa", "--email", "alice@example.com", stdout=StringIO())

        assert Authenticator.objects.filter(user=alice).count() == 0

    def test_missing_email_raises_command_error(self):
        """A typo in ``--email`` must fail loudly, not silently return 0.

        Silent success on a typo is the nightmare scenario during an
        incident — the operator walks away thinking they fixed it, the
        user still can't log in, and nobody knows why. We also pin the
        hint about ``account_emailaddress`` because that divergence is
        exactly what we hit in production during the 2026-04-16 incident.
        """
        with pytest.raises(CommandError) as exc_info:
            call_command("clear_mfa", "--email", "typo@example.com")
        assert "typo@example.com" in str(exc_info.value)
        assert "account_emailaddress" in str(exc_info.value)

    def test_user_with_no_authenticators_is_a_warning_not_an_error(self):
        """A user who exists but has no MFA rows is a no-op, not a failure.

        During mass key-rotation clears, the majority of users have never
        enrolled MFA — failing the command on each one would turn a batch
        run into a painful edge-case loop.
        """
        _make_user("nomfa@example.com", "nomfa")
        stdout = StringIO()

        call_command("clear_mfa", "--email", "nomfa@example.com", stdout=stdout)

        assert "nothing to delete" in stdout.getvalue().lower()


# ── Selector: --user-id ────────────────────────────────────────────────
# The user-id path exists so we can clear a user whose email itself is
# suspect or changing — e.g. email typos like the 2026-04-16 incident.


class TestClearByUserId:
    """``--user-id`` is the email-free alternative selector."""

    def test_deletes_only_target_users_rows(self):
        """Scoping by pk works the same as scoping by email.

        Symmetry with the email path keeps operator muscle memory simple:
        whichever selector they reach for, the semantics are identical.
        """
        alice = _make_user("alice@example.com", "alice")
        bob = _make_user("bob@example.com", "bob")
        alice_auth = _make_authenticator(alice)
        bob_auth = _make_authenticator(bob)

        call_command("clear_mfa", "--user-id", str(alice.pk), stdout=StringIO())

        assert not Authenticator.objects.filter(pk=alice_auth.pk).exists()
        assert Authenticator.objects.filter(pk=bob_auth.pk).exists()

    def test_missing_user_id_raises_command_error(self):
        """A nonexistent pk fails loudly, matching the ``--email`` behaviour."""
        with pytest.raises(CommandError) as exc_info:
            call_command("clear_mfa", "--user-id", "999999")
        assert "999999" in str(exc_info.value)


# ── Selector: --all-users ──────────────────────────────────────────────
# The mass-clear case is rare (major key rotation events) but when it
# happens, it needs to actually clear everything — not just the first
# user, not just some arbitrary subset.


class TestClearAllUsers:
    """``--all-users`` wipes every ``Authenticator`` row."""

    def test_clears_every_users_authenticators(self):
        """Post-run, the ``mfa_authenticator`` table is empty.

        This is a deliberately aggressive operation and the explicit
        flag name (``--all-users``, not ``--force`` or ``-a``) is the
        safeguard — typing it by accident is implausible.
        """
        alice = _make_user("alice@example.com", "alice")
        bob = _make_user("bob@example.com", "bob")
        _make_authenticator(alice)
        _make_authenticator(bob, auth_type="recovery_codes")

        call_command("clear_mfa", "--all-users", stdout=StringIO())

        assert Authenticator.objects.count() == 0


# ── Safety rails ───────────────────────────────────────────────────────
# Mistakes during an incident are high-cost. The selector group and
# --dry-run exist specifically to reduce blast radius.


class TestSelectorMutualExclusion:
    """Exactly one selector must be supplied — argparse enforces this.

    When invoked from a real shell, bad args exit via ``SystemExit`` (the
    standard argparse behaviour). When invoked via ``call_command()``,
    Django's ``CommandParser.error()`` re-raises as ``CommandError``
    instead so tests don't kill the test process. We assert on
    ``CommandError`` here; the production CLI behaviour is unchanged.
    """

    def test_no_selector_raises_command_error(self):
        """Running with no selector must fail, not fall through to a
        default of "delete everything".

        ``required=True`` on the mutually-exclusive group is the only thing
        stopping an empty invocation from landing in ``handle()`` with
        every selector falsy — which, if the branching in
        :meth:`_select_queryset` ever changed, could become
        "delete all authenticators". We pin the gate here.
        """
        with pytest.raises(CommandError):
            call_command("clear_mfa", stderr=StringIO())

    def test_combining_selectors_raises_command_error(self):
        """``--email`` AND ``--user-id`` together must fail.

        Accepting both would force an arbitrary tie-break rule (prefer
        email? prefer user-id?) that an operator could easily get wrong
        under pressure. Refusing both is the unambiguous choice.
        """
        with pytest.raises(CommandError):
            call_command(
                "clear_mfa",
                "--email",
                "alice@example.com",
                "--user-id",
                "1",
                stderr=StringIO(),
            )


class TestDryRun:
    """``--dry-run`` previews the effect without touching the database."""

    def test_dry_run_does_not_delete_rows(self):
        """A dry-run with real matches leaves the DB untouched.

        If dry-run silently deleted anyway, operators would lose trust in
        the safety rail and skip running it — defeating its purpose.
        """
        alice = _make_user("alice@example.com", "alice")
        alice_auth = _make_authenticator(alice)

        stdout = StringIO()
        call_command(
            "clear_mfa",
            "--email",
            "alice@example.com",
            "--dry-run",
            stdout=stdout,
        )

        assert Authenticator.objects.filter(pk=alice_auth.pk).exists()
        assert "[dry-run]" in stdout.getvalue()

    def test_dry_run_reports_what_would_be_deleted(self):
        """The preview lists each row that would be removed.

        Without this, ``--dry-run`` would say "would delete 1 row" and
        leave the operator guessing which row — useless during an
        incident when there might be stale test rows mixed in.
        """
        alice = _make_user("alice@example.com", "alice")
        _make_authenticator(alice, auth_type="totp")
        _make_authenticator(alice, auth_type="recovery_codes")

        stdout = StringIO()
        call_command(
            "clear_mfa",
            "--email",
            "alice@example.com",
            "--dry-run",
            stdout=stdout,
        )

        output = stdout.getvalue()
        assert "totp" in output
        assert "recovery_codes" in output
