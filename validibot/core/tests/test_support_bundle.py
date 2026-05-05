"""Tests for the support bundle redaction rules.

The support bundle is what operators send to support@validibot.com when
something breaks. The redaction contract is therefore load-bearing in a
trust sense: a leaked secret in a bundle is a real incident, even if the
operator was just trying to get help.

These tests pin the contract:

1. **Name-based redaction** — settings whose names match a sensitive
   fragment are redacted regardless of value type.
2. **Allowlist exceptions** — ``USE_AUTH``, ``AUTH_USER_MODEL``, etc.
   pass through despite matching ``AUTH``.
3. **Value-shape redaction** — defense-in-depth: settings that *look*
   like credentials (PEM, JWT, Bearer, embedded URL auth) are
   redacted even if the name didn't trip the first check.
4. **Schema versioning** — ``SUPPORT_BUNDLE_SCHEMA_VERSION`` is a
   Pydantic Literal, rejecting ``v2`` at parse time.
5. **Frozen + ``extra='forbid'``** — accidental field additions or
   mutations raise rather than silently propagating.

These are pure unit tests on the redaction primitives. Tests for the
``collect_support_bundle`` management command live alongside in a sibling
file because they need Django's test database for migration recording.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from validibot.core.support_bundle import REDACTED_SENTINEL
from validibot.core.support_bundle import SUPPORT_BUNDLE_SCHEMA_VERSION
from validibot.core.support_bundle import MigrationState
from validibot.core.support_bundle import OutboundCallStatus
from validibot.core.support_bundle import RedactedSetting
from validibot.core.support_bundle import SupportBundleAppSnapshot
from validibot.core.support_bundle import VersionInfo
from validibot.core.support_bundle import is_sensitive_setting
from validibot.core.support_bundle import looks_like_secret_value
from validibot.core.support_bundle import redact_setting_value

# ──────────────────────────────────────────────────────────────────────
# Name-based redaction
# ──────────────────────────────────────────────────────────────────────


class TestIsSensitiveSetting:
    """Setting names that signal credentials are flagged for redaction."""

    @pytest.mark.parametrize(
        "name",
        [
            "DJANGO_SECRET_KEY",
            "SECRET_KEY",
            "DATABASE_PASSWORD",
            "POSTGRES_PASSWORD",
            "OIDC_CLIENT_SECRET",
            "API_KEY",
            "AWS_API_KEY",
            "GITHUB_TOKEN",
            "WORKER_API_TOKEN",
            "SIGNING_KEY_PATH",
            "MFA_ENCRYPTION_KEY",
            "SENTRY_DSN",
            "STRIPE_WEBHOOK_SECRET",
            "AUTH_TOKEN",
            "GOOGLE_CREDENTIALS",
            "PRIVATE_KEY_PEM",
            "PASSWD_HASH",
        ],
    )
    def test_credential_name_triggers_redaction(self, name):
        assert is_sensitive_setting(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "DEBUG",
            "ALLOWED_HOSTS",
            "TIME_ZONE",
            "DEPLOYMENT_TARGET",
            "INSTALLED_APPS",
            "DATA_STORAGE_ROOT",
            "EMAIL_HOST",
            "VALIDIBOT_VERSION",
            "USE_TZ",
        ],
    )
    def test_non_credential_name_passes(self, name):
        assert is_sensitive_setting(name) is False

    @pytest.mark.parametrize(
        "name",
        [
            "USE_AUTH",
            "AUTH_USER_MODEL",
            "AUTHENTICATION_BACKENDS",
            "PASSWORD_HASHERS",
            "AUTH_PASSWORD_VALIDATORS",
        ],
    )
    def test_allowlisted_exceptions_pass_despite_fragment_match(self, name):
        """Names like ``USE_AUTH`` match ``AUTH`` but aren't credentials."""
        assert is_sensitive_setting(name) is False


# ──────────────────────────────────────────────────────────────────────
# Value-shape redaction (defense in depth)
# ──────────────────────────────────────────────────────────────────────


class TestLooksLikeSecretValue:
    """Values whose shape suggests credentials are flagged."""

    # ``detect-private-key`` pre-commit hook excludes this file
    # explicitly — see .pre-commit-config.yaml. The synthetic PEM
    # strings below carry zero real key material; they exist purely
    # to exercise the redaction regex.
    @pytest.mark.parametrize(
        "value",
        [
            # Long hex (SHA-256ish secret keys)
            "abc123def456abc123def456abc123def456",
            "f" * 64,
            # JWT
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0In0.abc",
            # PEM private key (with key-type prefix and without)
            "-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----",
            "-----BEGIN PRIVATE KEY-----\nMIIB...",
            # Bearer token
            "Bearer abc123def456",
            "bearer Eyj.token.here",
            # Connection string with embedded auth
            "postgres://user:pass@host:5432/db",
            "https://user:secret@api.example.com/path",
        ],
    )
    def test_secret_shapes_are_flagged(self, value):
        assert looks_like_secret_value(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "hello world",
            "validibot",
            "False",
            "/srv/validibot/data",
            "https://api.example.com/path",  # no embedded auth
            "0.4.0",
            "abc123",  # too short for hex pattern
            "",
        ],
    )
    def test_normal_values_pass(self, value):
        assert looks_like_secret_value(value) is False

    def test_non_string_returns_false(self):
        """Non-string values can't match string patterns."""
        assert looks_like_secret_value(42) is False
        assert looks_like_secret_value(True) is False  # noqa: FBT003 — testing arg type
        assert looks_like_secret_value([]) is False
        assert looks_like_secret_value({}) is False
        assert looks_like_secret_value(None) is False


# ──────────────────────────────────────────────────────────────────────
# redact_setting_value: name + value combined
# ──────────────────────────────────────────────────────────────────────


class TestRedactSettingValue:
    """The name+value redaction is the public single-call API."""

    def test_name_match_redacts_regardless_of_value_type(self):
        # A sensitive name redacts even if the value is a list / dict /
        # nothing-that-looks-like-a-secret.
        assert redact_setting_value("API_KEY", "anything") == REDACTED_SENTINEL
        assert (
            redact_setting_value("DATABASE_PASSWORD", ["x", "y"]) == REDACTED_SENTINEL
        )
        assert redact_setting_value("SECRET_KEY", {"sub": "ok"}) == REDACTED_SENTINEL

    def test_value_shape_redacts_when_name_doesnt(self):
        """A value-shape match catches 'innocent' setting names with secret bodies."""
        # ``DEBUG_HOOK`` doesn't match a sensitive fragment by name —
        # but the value's a JWT. Redact via the second-pass check.

        synthetic_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0In0.abc"
        assert redact_setting_value("DEBUG_HOOK", synthetic_jwt) == REDACTED_SENTINEL

    def test_normal_value_passes(self):
        # Booleans pass through unchanged — assert by equality, not by
        # ``is`` (ruff FBT003 flags boolean literals in positional args).
        assert redact_setting_value("DEBUG", value=False) is False
        assert redact_setting_value("ALLOWED_HOSTS", ["example.com"]) == [
            "example.com",
        ]
        assert (
            redact_setting_value("DATA_STORAGE_ROOT", "/srv/validibot/data")
            == "/srv/validibot/data"
        )

    def test_explicit_exception_passes_through(self):
        """``USE_AUTH=True`` is not a credential."""
        assert redact_setting_value("USE_AUTH", value=True) is True


# ──────────────────────────────────────────────────────────────────────
# Schema version
# ──────────────────────────────────────────────────────────────────────


class TestSchemaVersion:
    """The schema version is pinned and rejects unknown values."""

    def _minimal_kwargs(self) -> dict:
        return {
            "captured_at": "2026-05-05T12:00:00Z",
            "versions": VersionInfo(
                validibot_version="0.4.0",
                python_version="3.13.1",
                postgres_server_version="16.0",
                target="self_hosted",
            ),
            "migrations": MigrationState(head={}),
            "settings": [],
            "outbound_calls": OutboundCallStatus(
                sentry_enabled=False,
                posthog_enabled=False,
                email_configured=False,
                runtime_license_check_enabled=False,
            ),
        }

    def test_default_schema_version_is_v1(self):
        snapshot = SupportBundleAppSnapshot(**self._minimal_kwargs())
        assert snapshot.schema_version == "validibot.support-bundle.v1"
        assert snapshot.schema_version == SUPPORT_BUNDLE_SCHEMA_VERSION

    def test_rejects_other_schema_versions(self):
        """Future-proofing: a v2 schema deserialized by v1 code fails fast."""
        with pytest.raises(ValidationError):
            SupportBundleAppSnapshot(
                **self._minimal_kwargs(),
                schema_version="validibot.support-bundle.v2",
            )


# ──────────────────────────────────────────────────────────────────────
# Strict shape (frozen + extra='forbid')
# ──────────────────────────────────────────────────────────────────────


class TestStrictShape:
    """Snapshots are frozen and reject unknown fields."""

    def _minimal_kwargs(self) -> dict:
        return {
            "captured_at": "2026-05-05T12:00:00Z",
            "versions": VersionInfo(
                validibot_version="0.4.0",
                python_version="3.13.1",
                postgres_server_version="16.0",
                target="self_hosted",
            ),
            "migrations": MigrationState(head={}),
            "settings": [],
            "outbound_calls": OutboundCallStatus(
                sentry_enabled=False,
                posthog_enabled=False,
                email_configured=False,
                runtime_license_check_enabled=False,
            ),
        }

    def test_unknown_top_level_field_rejected(self):
        with pytest.raises(ValidationError):
            SupportBundleAppSnapshot(
                **self._minimal_kwargs(),
                surprise_field="not allowed",
            )

    def test_snapshot_is_frozen(self):
        """``frozen=True`` means re-assignment fails — snapshots are immutable."""
        snapshot = SupportBundleAppSnapshot(**self._minimal_kwargs())
        with pytest.raises(ValidationError):
            snapshot.captured_at = "tampered"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────
# RedactedSetting round-trip
# ──────────────────────────────────────────────────────────────────────


class TestRedactedSetting:
    """The ``RedactedSetting`` row preserves type for non-redacted values."""

    def test_non_redacted_preserves_type(self):
        """``DEBUG=False`` round-trips as bool, not string."""
        s = RedactedSetting(name="DEBUG", value=False, redacted=False)
        assert s.value is False
        assert s.redacted is False

    def test_redacted_value_is_sentinel(self):
        s = RedactedSetting(name="SECRET_KEY", value=REDACTED_SENTINEL, redacted=True)
        assert s.value == REDACTED_SENTINEL
        assert s.redacted is True

    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError):
            RedactedSetting(
                name="X",
                value="x",
                redacted=False,
                surprise="bad",
            )
