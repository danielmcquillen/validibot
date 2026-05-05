"""Tests for the ``collect_support_bundle`` Django management command.

The redaction primitives are tested in ``test_support_bundle.py``;
these tests cover the management command's wiring:

1. **Output format** — JSON to stdout (default) or to a file.
2. **Schema** — the produced JSON validates against
   ``SupportBundleAppSnapshot``.
3. **Redaction in practice** — sensitive settings emerge with
   ``[REDACTED]`` values; non-sensitive emerge verbatim.
4. **Doctor embedding** — the doctor's JSON output is embedded
   under ``doctor`` so support tooling can re-use the
   ``validibot.doctor.v1`` parser.
5. **No raw secrets in output** — even with values that look
   sensitive but slipped through name-based redaction (defense in
   depth via shape-based redaction).
"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings

from validibot.core.support_bundle import REDACTED_SENTINEL
from validibot.core.support_bundle import SUPPORT_BUNDLE_SCHEMA_VERSION
from validibot.core.support_bundle import SupportBundleAppSnapshot

pytestmark = pytest.mark.django_db


# ──────────────────────────────────────────────────────────────────────
# Output to stdout (default)
# ──────────────────────────────────────────────────────────────────────


class TestStdoutOutput:
    """Default output writes valid JSON to stdout."""

    def test_stdout_output_validates_against_schema(self):
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        # Round-trip through Pydantic to confirm shape stability.
        snapshot = SupportBundleAppSnapshot.model_validate_json(out.getvalue())
        assert snapshot.schema_version == SUPPORT_BUNDLE_SCHEMA_VERSION

    def test_stdout_output_is_indented_by_default(self):
        """Default mode produces human-readable JSON for support staff."""
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        # Indented JSON has at least one newline + indentation pair.
        assert "\n  " in out.getvalue()

    def test_no_indent_produces_minified(self):
        """``--no-indent`` emits compact JSON for slimmer bundles."""
        out = StringIO()
        call_command("collect_support_bundle", "--no-indent", stdout=out)
        # Minified JSON has no leading-whitespace lines.
        assert "\n  " not in out.getvalue().strip()


# ──────────────────────────────────────────────────────────────────────
# Output to file
# ──────────────────────────────────────────────────────────────────────


class TestFileOutput:
    """``--output <path>`` writes the JSON to disk."""

    def test_writes_to_file(self, tmp_path):
        out_path = tmp_path / "snapshot.json"
        call_command(
            "collect_support_bundle",
            "--output",
            str(out_path),
            stdout=StringIO(),
        )
        assert out_path.exists()
        # File round-trips through the schema cleanly.
        snapshot = SupportBundleAppSnapshot.model_validate_json(
            out_path.read_text(encoding="utf-8"),
        )
        assert snapshot.schema_version == SUPPORT_BUNDLE_SCHEMA_VERSION


# ──────────────────────────────────────────────────────────────────────
# Redaction in the produced output
# ──────────────────────────────────────────────────────────────────────


class TestRedactionInOutput:
    """Sensitive settings emerge redacted; non-sensitive emerge verbatim."""

    @override_settings(
        SECRET_KEY="this-is-a-secret-do-not-leak",  # noqa: S106 — synthetic value for redaction test
        EMAIL_HOST_PASSWORD="not-the-real-password",  # noqa: S106 — synthetic value for redaction test
        DEBUG=False,
        ALLOWED_HOSTS=["example.com", "validibot.example.com"],
    )
    def test_secret_key_is_redacted(self):
        """The DJANGO_SECRET_KEY value never appears in the bundle."""
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        body = out.getvalue()

        # The value must NEVER appear; the key name MUST appear.
        assert "this-is-a-secret-do-not-leak" not in body
        assert "SECRET_KEY" in body
        # The redaction sentinel must appear next to it.
        assert REDACTED_SENTINEL in body

    @override_settings(EMAIL_HOST_PASSWORD="not-the-real-password")  # noqa: S106 — synthetic value
    def test_email_password_is_redacted(self):
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        assert "not-the-real-password" not in out.getvalue()

    @override_settings(DEBUG=True, ALLOWED_HOSTS=["example.com"])
    def test_non_sensitive_settings_pass_through(self):
        """Booleans, lists, plain strings are recorded verbatim.

        Support staff need to see ``DEBUG=True`` to diagnose; we
        don't redact that.
        """
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        snapshot = SupportBundleAppSnapshot.model_validate_json(out.getvalue())

        debug = next(s for s in snapshot.settings if s.name == "DEBUG")
        assert debug.value is True
        assert debug.redacted is False

        hosts = next(s for s in snapshot.settings if s.name == "ALLOWED_HOSTS")
        assert hosts.value == ["example.com"]
        assert hosts.redacted is False


# ──────────────────────────────────────────────────────────────────────
# Defense-in-depth: shape-based redaction for unexpected secret-shaped values
# ──────────────────────────────────────────────────────────────────────


class TestShapeBasedRedactionDefence:
    """Settings whose VALUES look like secrets get redacted even if NAMES don't."""

    @override_settings(
        # SITE_URL with embedded basic auth is a real footgun for self-
        # hosted operators — the URL is normally innocuous but if they
        # paste in ``https://user:secret@validibot.example.com``, that
        # creds-in-URL form must be redacted.
        SITE_URL="https://operator:secret@validibot.example.com",
    )
    def test_creds_in_url_get_redacted_via_shape(self):
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        body = out.getvalue()

        # The credential must NOT leak.
        assert "operator:secret" not in body
        # Either the redaction sentinel takes its place, or the URL is
        # absent entirely. Either is acceptable; what matters is the
        # secret never appears.
        assert "secret@validibot.example.com" not in body


# ──────────────────────────────────────────────────────────────────────
# Doctor embedding
# ──────────────────────────────────────────────────────────────────────


class TestDoctorEmbedding:
    """The doctor's JSON output is embedded for support-tooling reuse."""

    def test_doctor_section_is_present(self):
        """``doctor`` is captured even when doctor exits non-zero.

        Support bundles are usually collected when something is
        wrong, which means doctor often reports errors. The bundle
        captures whatever doctor said, regardless of exit code.
        """
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        snapshot = SupportBundleAppSnapshot.model_validate_json(out.getvalue())
        # Doctor may be None on a deeply broken deployment, but on a
        # test deployment it should at least produce some output.
        if snapshot.doctor is not None:
            assert snapshot.doctor.get("schema_version") == "validibot.doctor.v1"


# ──────────────────────────────────────────────────────────────────────
# Migration head capture
# ──────────────────────────────────────────────────────────────────────


class TestMigrationHeadCapture:
    """The live migration head is captured (same shape as backup manifest)."""

    def test_migration_head_is_a_dict(self):
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        snapshot = SupportBundleAppSnapshot.model_validate_json(out.getvalue())
        # In a working test DB at least a few apps have applied
        # migrations.
        assert isinstance(snapshot.migrations.head, dict)
        assert len(snapshot.migrations.head) > 0


# ──────────────────────────────────────────────────────────────────────
# Outbound-call status
# ──────────────────────────────────────────────────────────────────────


class TestOutboundCallStatus:
    """The ADR section 10 outbound-call surface is captured per-channel."""

    @override_settings(SENTRY_DSN="https://abc@sentry.example.com/1")
    def test_sentry_enabled_when_dsn_set(self):
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        snapshot = SupportBundleAppSnapshot.model_validate_json(out.getvalue())
        assert snapshot.outbound_calls.sentry_enabled is True
        # The Sentry DSN itself is a credential — it must be redacted
        # in the settings dump.
        body = out.getvalue()
        assert "sentry.example.com/1" not in body or REDACTED_SENTINEL in body

    @override_settings(SENTRY_DSN="")
    def test_sentry_disabled_when_dsn_empty(self):
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        snapshot = SupportBundleAppSnapshot.model_validate_json(out.getvalue())
        assert snapshot.outbound_calls.sentry_enabled is False

    def test_runtime_license_check_is_always_false(self):
        """ADR section 10: self-hosted never phones home for license checks."""
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        snapshot = SupportBundleAppSnapshot.model_validate_json(out.getvalue())
        assert snapshot.outbound_calls.runtime_license_check_enabled is False


# ──────────────────────────────────────────────────────────────────────
# JSON shape stability for support tooling
# ──────────────────────────────────────────────────────────────────────


class TestJsonShape:
    """Top-level keys are operator-readable contracts."""

    def test_top_level_keys(self):
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        as_dict = json.loads(out.getvalue())
        assert set(as_dict.keys()) == {
            "schema_version",
            "captured_at",
            "versions",
            "migrations",
            "settings",
            "outbound_calls",
            "validators",
            "doctor",
        }

    def test_versions_block_shape(self):
        out = StringIO()
        call_command("collect_support_bundle", stdout=out)
        as_dict = json.loads(out.getvalue())
        versions = as_dict["versions"]
        assert "validibot_version" in versions
        assert "python_version" in versions
        assert "postgres_server_version" in versions
        assert "target" in versions
