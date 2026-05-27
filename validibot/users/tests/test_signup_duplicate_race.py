"""Tests for the duplicate-username race fix in ``UserSignupForm.try_save``.

Background
----------
Allauth's stock signup flow validates uniqueness against the database
with ``User.objects.filter(username=value).exists()`` inside
``clean_username``. That check is correct in single-request isolation
but creates a TOCTOU race when two simultaneous signup POSTs submit
the same username: both pass ``is_valid()`` (neither row exists yet),
then one INSERT succeeds and the other raises
``psycopg.errors.UniqueViolation``, which Django wraps as
``django.db.utils.IntegrityError``.

Before the fix, that ``IntegrityError`` escaped the signup view as
an HTTP 500, paged Sentry, and gave the losing user a generic error
page. ``UserSignupForm.try_save`` now catches the exception, maps
the offending constraint to a form field, and returns the rendered
signup template with a friendly field error.

What this suite covers
----------------------
1. Known signup constraints map to field-level form errors.
2. Unrelated ``IntegrityError`` (a different constraint) propagates
   so genuine integrity bugs surface loudly.
3. The constraint-name lookup uses ``__cause__.diag.constraint_name``
   first and falls back to scanning the exception text — both code
   paths are exercised here.
4. ``_signup_integrity_error_field_message`` returns ``None`` for
   non-uniqueness errors (e.g. NOT NULL violations) so the caller
   knows to re-raise.

These tests do not attempt to trigger a real race — that would
require multi-process orchestration with deliberate timing windows.
Instead we simulate the symptom: patch ``super().try_save`` to raise
an ``IntegrityError`` shaped like the production one, and assert the
form converts it correctly. The race itself is a property of any
"check then insert" web flow without serialisable transactions; the
fix is to handle the inevitable loser gracefully, which is what we
verify.
"""

from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import IntegrityError
from django.test import RequestFactory

from validibot.users.forms import SIGNUP_UNIQUE_CONSTRAINT_ERRORS
from validibot.users.forms import UserSignupForm
from validibot.users.forms import _signup_integrity_error_field_message

# ── Helpers ───────────────────────────────────────────────────────────


def _build_integrity_error(
    *,
    constraint_name: str | None,
    message: str = "duplicate key value violates unique constraint",
) -> IntegrityError:
    """Construct an ``IntegrityError`` that looks like the production one.

    Production ``IntegrityError`` instances raised from a psycopg
    UniqueViolation chain the original psycopg error onto
    ``__cause__``, where ``.diag.constraint_name`` carries the
    constraint name. We replicate that shape so the helper under test
    walks the same attribute path it walks in production.

    Passing ``constraint_name=None`` produces an exception with no
    ``__cause__`` chain (the fallback path that scans
    ``str(exc)`` for known constraint names).
    """
    exc = IntegrityError(message)
    if constraint_name is not None:
        # Simulate ``psycopg.errors.UniqueViolation`` via a tiny stub.
        # ``getattr(... , "diag", None)`` is the only attribute the
        # helper reads, so a SimpleNamespace is enough.
        cause = Exception(f"duplicate constraint {constraint_name}")
        cause.diag = SimpleNamespace(constraint_name=constraint_name)  # type: ignore[attr-defined]
        exc.__cause__ = cause
    return exc


_VALID_POST_DATA = {
    "username": "alfred",
    "email": "alfred@example.com",
    "password1": "Str0ngPassw0rd!",
    "password2": "Str0ngPassw0rd!",
}


# ── Direct tests for the helper function ──────────────────────────────


class TestSignupIntegrityErrorFieldMessage:
    """Unit tests for :func:`_signup_integrity_error_field_message`.

    These bypass the form layer entirely and exercise the helper's two
    detection paths (``__cause__.diag.constraint_name`` and message
    text scan) plus the negative case.
    """

    def test_known_constraint_via_diag_returns_field_and_message(self):
        """The happy path: psycopg sets ``diag.constraint_name`` and
        the helper looks it up directly. This is the production path.
        """
        exc = _build_integrity_error(constraint_name="users_user_username_key")

        result = _signup_integrity_error_field_message(exc)

        assert result is not None
        field, message = result
        assert field == "username"
        # Message is a lazy gettext object; coerce to str for the assert.
        assert "username" in str(message).lower()
        assert "already exists" in str(message).lower()

    def test_known_constraint_via_text_fallback_returns_field_and_message(
        self,
    ):
        """When ``__cause__`` is missing (older psycopg, or a wrapped
        exception that didn't chain), the helper scans the exception
        text for known constraint names. This is the resilience path.
        """
        # No ``__cause__`` chain. The constraint name is only in the
        # message text — the helper must still find it.
        exc = IntegrityError(
            'duplicate key value violates unique constraint "users_user_username_key"',
        )

        result = _signup_integrity_error_field_message(exc)

        assert result is not None
        field, _ = result
        assert field == "username"

    def test_unknown_constraint_returns_none(self):
        """Constraints we don't recognise return ``None`` so the
        caller knows to re-raise. This is the safety property: an
        unexpected integrity bug must not be silently swallowed.
        """
        exc = _build_integrity_error(
            constraint_name="some_unrelated_constraint_we_dont_know",
        )

        assert _signup_integrity_error_field_message(exc) is None

    def test_non_unique_violation_returns_none(self):
        """A NOT NULL violation (or any other IntegrityError that
        isn't a known signup uniqueness conflict) returns ``None``.

        We don't want the form to dress up an arbitrary integrity bug
        as a friendly username error — that would mask real schema
        problems behind a misleading UI.
        """
        # Realistic Postgres NOT NULL violation message; no known
        # constraint name appears anywhere in the chain.
        exc = IntegrityError(
            'null value in column "email" violates not-null constraint',
        )

        assert _signup_integrity_error_field_message(exc) is None

    def test_constraint_name_dispatch_table_has_username_entry(self):
        """Pin the dispatch table's invariants so a future edit that
        accidentally removes the username row fails this test instead
        of silently allowing 500s back into production.
        """
        assert "users_user_username_key" in SIGNUP_UNIQUE_CONSTRAINT_ERRORS
        field, _ = SIGNUP_UNIQUE_CONSTRAINT_ERRORS["users_user_username_key"]
        assert field == "username"


# ── Form-level tests for try_save ─────────────────────────────────────


@pytest.mark.django_db
class TestUserSignupFormTrySave:
    """End-to-end tests for ``UserSignupForm.try_save``.

    Each test stubs ``super().try_save`` so we control exactly which
    exception (if any) is raised, then asserts the form's behaviour:
    field error attached, ``(None, response)`` returned, response
    body contains the expected error message.
    """

    def _bind_and_validate(self) -> UserSignupForm:
        """Return a bound, validated ``UserSignupForm`` ready for
        ``try_save``. Validation must pass — the bug only manifests
        on the INSERT step, which runs after ``is_valid``.
        """
        form = UserSignupForm(data=_VALID_POST_DATA)
        # ``is_valid`` populates ``cleaned_data`` and lets ``add_error``
        # mutate the right state. Calling it here matches the live flow:
        # the view runs ``form.is_valid()`` before ``form.try_save()``.
        assert form.is_valid(), form.errors
        return form

    def test_known_duplicate_constraint_renders_signup_with_field_error(self):
        """A known duplicate-username constraint must be converted to a
        ``(None, response)`` tuple with a username field error on the
        form. The response is a rendered signup template the view
        returns directly.
        """
        form = self._bind_and_validate()
        request = RequestFactory().post("/accounts/signup/", _VALID_POST_DATA)
        # Add a session — the cloud signup view writes to it earlier in
        # the request lifecycle, and ``render`` may consult middleware.
        request.session = {}

        # Mock the parent's ``try_save`` to raise as if Postgres just
        # rejected the INSERT. The exception shape matches the
        # production traceback exactly.
        with patch(
            "allauth.account.forms.SignupForm.try_save",
            side_effect=_build_integrity_error(
                constraint_name="users_user_username_key",
            ),
        ):
            user, response = form.try_save(request)

        assert user is None
        assert response is not None
        # ``add_error`` marks the form invalid and attaches to the
        # named field. Both are observable side effects we want pinned.
        assert "username" in form.errors
        assert any(
            "already exists" in str(msg).lower() for msg in form.errors["username"]
        )
        # 409 Conflict is the semantically-correct status for a
        # duplicate-resource POST. Pinning it documents the contract.
        assert response.status_code == HTTPStatus.CONFLICT

    def test_unknown_constraint_propagates_integrity_error(self):
        """An ``IntegrityError`` whose constraint name isn't in the
        dispatch table must propagate out of ``try_save`` unchanged.

        This is the safety guarantee: unexpected integrity bugs
        (someone adds a new ``unique=True`` field and forgets to
        register it) keep raising 500s and paging Sentry, so the
        problem gets noticed. Silently rendering a generic form
        error would mask the bug.
        """
        form = self._bind_and_validate()
        request = RequestFactory().post("/accounts/signup/", _VALID_POST_DATA)
        request.session = {}

        unknown = _build_integrity_error(
            constraint_name="users_user_some_new_field_key",
        )

        with (
            patch(
                "allauth.account.forms.SignupForm.try_save",
                side_effect=unknown,
            ),
            pytest.raises(IntegrityError),
        ):
            form.try_save(request)

        # The form should NOT have been mutated — no errors attached,
        # because we re-raised before reaching ``add_error``.
        assert "username" not in form.errors

    def test_successful_save_passes_through_unchanged(self):
        """When ``super().try_save`` succeeds, our override must
        return its ``(user, response)`` tuple verbatim. The override
        is a try/except shim — the happy path must be a straight
        passthrough.
        """
        form = self._bind_and_validate()
        request = RequestFactory().post("/accounts/signup/", _VALID_POST_DATA)
        request.session = {}

        sentinel_user = object()
        sentinel_response = object()

        with patch(
            "allauth.account.forms.SignupForm.try_save",
            return_value=(sentinel_user, sentinel_response),
        ):
            user, response = form.try_save(request)

        assert user is sentinel_user
        assert response is sentinel_response
