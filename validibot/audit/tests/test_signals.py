"""Integration tests for the Session-2 audit capture points.

Verifies that each Phase-1 signal actually produces an ``AuditLogEntry``
with the expected action and attribution:

* ``LOGIN_SUCCEEDED`` — Django's ``user_logged_in`` on a real session
  login.
* ``LOGIN_FAILED`` — Django's ``user_login_failed`` on bad credentials,
  with the actor carrying no user but the attempted username in
  ``metadata``.
* ``PASSWORD_CHANGED`` — allauth's ``password_changed`` after a
  successful password update.
* ``API_KEY_CREATED`` / ``API_KEY_REVOKED`` — DRF ``Token`` post-save /
  post-delete, never exposing the raw ``key``.

The signals module is designed to read actor/request context from
``validibot.audit.context.get_current_context()``. That context is
populated by the middleware on HTTP requests, but signal tests usually
fire the signal directly — they don't go through an HTTP request.

We test both shapes: direct signal dispatch (handler works with the
empty fallback context) and signal dispatched from inside a middleware
context (handler picks up the actor + request_id).
"""

from __future__ import annotations

from django.contrib.auth.signals import user_logged_in
from django.contrib.auth.signals import user_login_failed
from django.http import HttpResponse
from django.test import RequestFactory
from django.test import TestCase
from rest_framework.authtoken.models import Token

from validibot.audit.constants import AuditAction
from validibot.audit.context import get_current_context
from validibot.audit.middleware import AuditContextMiddleware
from validibot.audit.models import AuditLogEntry
from validibot.users.tests.factories import UserFactory


class LoginSignalTests(TestCase):
    """LOGIN_SUCCEEDED and LOGIN_FAILED capture paths."""

    def setUp(self) -> None:
        """Each test starts from a clean audit table to avoid cross-
        contamination from fixtures created by other apps at import.
        """

        self.factory = RequestFactory()
        AuditLogEntry.objects.all().delete()

    def test_successful_login_produces_entry(self) -> None:
        """A ``user_logged_in`` signal (fired by Django's ``login()``)
        lands exactly one LOGIN_SUCCEEDED entry with the actor's user
        attached.
        """

        user = UserFactory()
        request = self.factory.get("/accounts/login/")

        user_logged_in.send(
            sender=user.__class__,
            request=request,
            user=user,
        )

        entries = list(
            AuditLogEntry.objects.filter(action=AuditAction.LOGIN_SUCCEEDED.value),
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].actor.user, user)
        # metadata.path records where the login happened — useful for
        # separating web UI logins from API-token-auth flows later.
        self.assertEqual(entries[0].metadata["path"], "/accounts/login/")
        self.assertEqual(entries[0].metadata["channel"], "web")

    def test_login_within_middleware_captures_request_id(self) -> None:
        """When the login fires inside an HTTP request the audit entry
        should carry the middleware-minted request id — that's what
        cross-references the DB entry to Cloud Logging markers.
        """

        user = UserFactory()
        captured_request_id: list[str] = []

        def view(request):
            """Dispatch the login signal from inside the view so
            ``get_current_context()`` is populated.
            """

            captured_request_id.append(get_current_context().request_id)
            user_logged_in.send(
                sender=user.__class__,
                request=request,
                user=user,
            )
            return HttpResponse()

        middleware = AuditContextMiddleware(view)
        request = self.factory.get("/accounts/login/")
        request.user = user
        middleware(request)

        entry = AuditLogEntry.objects.get(action=AuditAction.LOGIN_SUCCEEDED.value)
        self.assertEqual(entry.request_id, captured_request_id[0])
        self.assertTrue(entry.request_id.startswith("req_"))

    def test_failed_login_produces_entry_with_no_user(self) -> None:
        """``user_login_failed`` fires when credentials don't resolve.
        The actor row must NOT link to a user (there isn't one) but the
        attempted username should land in ``metadata`` so incident
        response can spot credential-stuffing patterns.
        """

        request = self.factory.post("/accounts/login/")
        user_login_failed.send(
            sender=None,
            credentials={"username": "ghost@example.com", "password": "wrong"},
            request=request,
        )

        entry = AuditLogEntry.objects.get(action=AuditAction.LOGIN_FAILED.value)
        self.assertIsNone(entry.actor.user)
        self.assertEqual(entry.metadata["attempted_username"], "ghost@example.com")
        # Critical: we must not have the password anywhere on the entry
        # (Django scrubs it from ``credentials`` before dispatch, but
        # we double-check — this is an explicit regression guard).
        entry_json = str(entry.changes) + str(entry.metadata)
        self.assertNotIn("wrong", entry_json)


class PasswordChangeSignalTests(TestCase):
    """PASSWORD_CHANGED via allauth's ``password_changed`` signal."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        AuditLogEntry.objects.all().delete()

    def test_password_change_emits_entry(self) -> None:
        """allauth fires ``password_changed`` from every password-
        update flow (user-initiated, reset, admin force). Every one
        produces an audit entry targeted at the user whose password
        changed.
        """

        # Import here so that even if allauth isn't configured the
        # rest of the file's tests can still import at collection time.
        from allauth.account.signals import password_changed

        user = UserFactory()
        request = self.factory.post("/accounts/password/change/")
        password_changed.send(
            sender=user.__class__,
            request=request,
            user=user,
        )

        entry = AuditLogEntry.objects.get(action=AuditAction.PASSWORD_CHANGED.value)
        self.assertEqual(entry.actor.user, user)
        # The target is the user themselves — changed their own password.
        self.assertEqual(entry.target_type, "users.User")
        self.assertEqual(entry.target_id, str(user.pk))


class ApiTokenSignalTests(TestCase):
    """API_KEY_CREATED and API_KEY_REVOKED via DRF Token lifecycle."""

    def setUp(self) -> None:
        AuditLogEntry.objects.all().delete()
        # Ensure the user has no prior tokens — ``Token.save()`` is
        # triggered by ``Token.objects.create`` which in turn fires
        # ``post_save`` and we want exactly one entry per test.
        Token.objects.all().delete()

    def test_token_creation_produces_entry_without_exposing_key(self) -> None:
        """Creating a ``Token`` row fires post_save(created=True)
        which our receiver translates into an ``API_KEY_CREATED``
        audit entry. The most important assertion: the raw token
        ``key`` must NEVER appear in the stored entry.
        """

        user = UserFactory()
        token = Token.objects.create(user=user)

        entry = AuditLogEntry.objects.get(action=AuditAction.API_KEY_CREATED.value)
        self.assertEqual(entry.target_type, "authtoken.Token")
        self.assertEqual(entry.target_id, str(token.pk))
        # Critical regression guard: the raw key is present in the
        # Token instance but must not be anywhere on the audit entry.
        self.assertNotIn(token.key, str(entry.changes or {}))
        self.assertNotIn(token.key, str(entry.metadata or {}))
        self.assertNotIn(token.key, entry.target_repr)

    def test_token_deletion_produces_revoke_entry(self) -> None:
        """Deleting the token fires ``post_delete`` → our receiver
        produces ``API_KEY_REVOKED``. The same no-key-leak guard
        applies.
        """

        user = UserFactory()
        token = Token.objects.create(user=user)
        # The post_save already wrote a CREATE entry — clear it so
        # we're asserting only about REVOKE.
        key_value = token.key
        AuditLogEntry.objects.all().delete()

        token.delete()

        entry = AuditLogEntry.objects.get(action=AuditAction.API_KEY_REVOKED.value)
        self.assertEqual(entry.target_type, "authtoken.Token")
        self.assertNotIn(key_value, str(entry.changes or {}))
        self.assertNotIn(key_value, str(entry.metadata or {}))
        self.assertNotIn(key_value, entry.target_repr)

    def test_token_update_does_not_produce_second_created_entry(self) -> None:
        """``post_save`` fires on both create and update. Our receiver
        ignores updates (created=False) so re-saving an existing
        Token row should not produce a second API_KEY_CREATED entry.
        """

        user = UserFactory()
        token = Token.objects.create(user=user)
        # Exactly one CREATED entry so far.
        self.assertEqual(
            AuditLogEntry.objects.filter(
                action=AuditAction.API_KEY_CREATED.value,
            ).count(),
            1,
        )

        token.save()  # triggers post_save(created=False)

        # Still exactly one — no second entry.
        self.assertEqual(
            AuditLogEntry.objects.filter(
                action=AuditAction.API_KEY_CREATED.value,
            ).count(),
            1,
        )
