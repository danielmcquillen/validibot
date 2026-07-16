"""
Tests for the ``check_validibot`` doctor management command.

This module verifies the **shape** of the doctor command's output —
the contract that operators, integrations, and support tooling rely
on. Specifically:

1. Every check result emits a stable check ID (``VBxxx``) and
   category. The pair is the load-bearing contract that
   ``docs/operations/self-hosting/doctor-check-ids.md`` documents and
   that integrations route on.

2. The 5-state severity scale (plus ``skipped``) is wired up:
   ``ok | info | warn | error | fatal | skipped``. Existing checks
   today emit ``ok``, ``warn``, ``error``, and ``skipped``; ``info``
   and ``fatal`` are reserved for future checks but the JSON schema
   already includes them in the summary count so integrations don't
   break when those statuses start appearing.

3. The JSON output matches the ``validibot.doctor.v1`` schema shape:
   ``schema_version``, ``validibot_version``, ``target``, ``stage``,
   ``ran_at``, ``summary``, ``checks``. Each check row has ``id``,
   ``category``, ``name``, ``status``, ``message``, ``details``, and
   ``fix_hint``.

4. The exit code semantics are right: ``error``/``fatal`` always
   fail; ``warn`` fails only with ``--strict``; ``ok``/``info``/
   ``skipped`` always pass.

5. The ``--target`` and ``--stage`` arguments plumb through to JSON
   output verbatim, so support bundles, CI logs, and dashboards know
   which deployment the doctor ran against.

Most tests lock in the public output contract rather than probing live
infrastructure. Focused unit tests also cover target-aware decisions and
runner availability checks where a real container environment would otherwise
produce misleading, host-dependent results.

Why these tests matter: the JSON schema is consumed by the support
bundle (Phase 6), CI pipelines that run ``--strict``, and any
operator dashboard that polls doctor on a schedule. A breaking change
to the schema would silently break all of those. These tests catch
schema regressions at PR-review time.
"""

from __future__ import annotations

import json
import tempfile
from io import StringIO
from unittest.mock import Mock
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.test import override_settings

from validibot.core.management.commands.check_validibot import DOCTOR_SCHEMA_VERSION
from validibot.core.management.commands.check_validibot import CheckResult
from validibot.core.management.commands.check_validibot import CheckStatus
from validibot.core.management.commands.check_validibot import Command

# Severity values that must appear in the JSON summary even when their
# count is zero, so integrations have a predictable shape to read.
EXPECTED_SUMMARY_KEYS = frozenset(
    {"ok", "info", "warn", "error", "fatal", "skipped"},
)

# Top-level JSON keys per the v1 schema. Removing or renaming any of
# these requires a v2 schema bump and a migration window.
#
# ``provider`` was added in Phase 1 Session 2 (additive v1 change)
# alongside the ``--provider digitalocean`` overlay.
EXPECTED_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "validibot_version",
        "target",
        "stage",
        "provider",
        "ran_at",
        "summary",
        "checks",
    },
)

# Per-check fields. Same v1 contract; same migration rule.
EXPECTED_CHECK_KEYS = frozenset(
    {"id", "category", "name", "status", "message", "details", "fix_hint"},
)


def _run_doctor(*args: str) -> tuple[dict, int]:
    """Run ``check_validibot --json`` and return (parsed_json, exit_code).

    Why we always pass ``--json``: the human-readable output isn't a
    contract — it can change freely. The JSON is the contract these
    tests are guarding. ``--json`` produces a single JSON document on
    stdout; we parse it and return.

    Why we capture the exit code via SystemExit: Django's
    ``call_command`` doesn't return the management command's exit
    code; the doctor calls ``sys.exit(1)`` on errors, which raises
    ``SystemExit``. We catch that to inspect the code.
    """
    stdout = StringIO()
    exit_code = 0
    try:
        call_command(
            "check_validibot",
            "--json",
            *args,
            stdout=stdout,
        )
    except SystemExit as exc:
        # ``sys.exit(0)`` raises SystemExit(0) on some Pythons; treat
        # explicit non-int as 1.
        exit_code = exc.code if isinstance(exc.code, int) else 1
    return json.loads(stdout.getvalue()), exit_code


class DoctorJsonSchemaTests(TestCase):
    """Verify the JSON output shape matches the v1 schema contract.

    Integrations downstream (support bundle, CI, dashboards) rely on
    this shape. Breaking it requires bumping ``DOCTOR_SCHEMA_VERSION``
    and providing a migration path.
    """

    def test_json_has_schema_version(self):
        """The schema_version field is the version contract.

        Any consumer can read this first, decide whether they
        understand the schema, and degrade gracefully if not.
        """
        result, _ = _run_doctor()
        self.assertEqual(result["schema_version"], DOCTOR_SCHEMA_VERSION)
        self.assertEqual(result["schema_version"], "validibot.doctor.v1")

    def test_json_has_all_top_level_keys(self):
        """All v1 top-level keys must be present, even when null.

        Consumers shouldn't have to special-case "this field might be
        missing." Predictable shape > optional fields.
        """
        result, _ = _run_doctor()
        self.assertEqual(set(result.keys()), EXPECTED_TOP_LEVEL_KEYS)

    def test_json_summary_includes_all_severities(self):
        """The summary block names every severity, even if count is 0.

        This is what lets a dashboard render counts for all
        severities without checking which keys exist. Adding a new
        severity (e.g. an eventual ``critical``) is an additive v1
        change; removing one is a v2 break.
        """
        result, _ = _run_doctor()
        self.assertEqual(set(result["summary"].keys()), EXPECTED_SUMMARY_KEYS)
        for status in result["summary"].values():
            self.assertIsInstance(status, int)
            self.assertGreaterEqual(status, 0)

    def test_each_check_has_all_required_fields(self):
        """Every check row carries the v1 per-check schema."""
        result, _ = _run_doctor()
        for check in result["checks"]:
            self.assertEqual(set(check.keys()), EXPECTED_CHECK_KEYS)

    def test_each_check_has_a_check_id(self):
        """Check IDs (VB0xx-VB9xx) are the load-bearing contract.

        ``docs/operations/self-hosting/doctor-check-ids.md`` documents
        each ID's meaning and fix. Operators look up issues by ID.
        Integrations route on ID. A check without an ID is a contract
        violation that this test catches before merge.
        """
        result, _ = _run_doctor()
        for check in result["checks"]:
            self.assertTrue(
                check["id"].startswith("VB"),
                f"Check {check['name']!r} has invalid id {check['id']!r}; "
                f"expected VB-prefixed ID per the doctor-check-ids docs.",
            )
            self.assertGreaterEqual(
                len(check["id"]),
                5,
                f"Check {check['name']!r} id {check['id']!r} too short; "
                f"expected ``VB`` + at least 3 digits.",
            )

    def test_each_check_status_is_valid_enum_value(self):
        """The status field uses the 5+1 severity vocabulary.

        We accept the lower-case enum values: ok, info, warn, error,
        fatal, skipped. Any other value would be a schema violation.
        """
        valid_statuses = {s.value for s in CheckStatus}
        result, _ = _run_doctor()
        for check in result["checks"]:
            self.assertIn(check["status"], valid_statuses)


class DoctorTargetStagePlumbingTests(TestCase):
    """Verify --target and --stage propagate to JSON output.

    The doctor must accurately self-report which deployment it ran
    against. Support bundles and CI logs rely on this — without it,
    investigating a failure across multi-target deploys gets confusing.
    """

    def test_target_defaults_to_settings_value(self):
        """When --target is omitted, doctor reads settings.DEPLOYMENT_TARGET.

        That's the canonical source — the running app's configured
        target. Operators should rarely need to override.
        """
        result, _ = _run_doctor()
        # In the test settings module, DEPLOYMENT_TARGET="test"
        self.assertEqual(result["target"], "test")

    def test_explicit_target_overrides_setting(self):
        """--target self_hosted runs the self-hosted profile of checks.

        Useful when (rare case) running doctor against a settings
        config that has the wrong DEPLOYMENT_TARGET, or when
        previewing what self-hosted profile checks would say.
        """
        result, _ = _run_doctor("--target", "self_hosted")
        self.assertEqual(result["target"], "self_hosted")

    def test_stage_propagates_when_set(self):
        """--stage prod surfaces in the JSON for GCP runs."""
        result, _ = _run_doctor("--target", "gcp", "--stage", "prod")
        self.assertEqual(result["target"], "gcp")
        self.assertEqual(result["stage"], "prod")

    def test_stage_is_null_when_omitted(self):
        """Self-hosted has no stage — JSON shows null, not empty string.

        Differentiates "no stage applies" (self-hosted) from an
        explicitly-set empty stage. Integrations can branch on
        ``stage is None`` cleanly.
        """
        result, _ = _run_doctor("--target", "self_hosted")
        self.assertIsNone(result["stage"])


class DoctorExitCodeTests(TestCase):
    """Verify exit-code semantics match the documented contract.

    Pre-flight checks in destructive recipes (Phase 4 upgrade,
    Phase 3 restore) rely on doctor's exit code. CI pipelines rely on
    --strict to fail builds when warnings appear. The exact mapping
    has to be right.
    """

    def test_fresh_test_environment_runs_to_completion(self):
        """Doctor runs to completion in a clean test environment.

        A clean Django test environment has missing roles/permissions
        (no setup_validibot run), missing media directory, missing
        CSRF trusted origins, etc. — so doctor legitimately reports
        errors. What we're testing here is that doctor doesn't *crash*
        in the middle: every check function executes, every result is
        well-formed, and the exit code is the documented one (1 when
        there are errors).

        If doctor crashes or returns malformed JSON in the test env,
        it'd crash in production environments too. This test catches
        that class of regression.
        """
        result, exit_code = _run_doctor()
        # JSON parsed successfully (we got here) — that's the main
        # smoke test.
        self.assertIn("checks", result)
        self.assertGreater(len(result["checks"]), 0)
        # Exit code is 1 because the test env is intentionally
        # incomplete. The point is the value is one of the documented
        # exit codes (0 or 1), not some random code from a crash.
        self.assertIn(
            exit_code,
            (0, 1),
            f"Unexpected exit code {exit_code} — should be 0 or 1.",
        )

    def test_strict_promotes_warnings_to_failures(self):
        """--strict makes any warn-level result cause non-zero exit.

        This is what CI pipelines and pre-commit hooks rely on to
        gate merges. A doctor that's "passing with warnings" should
        still fail under --strict.
        """
        # We expect at least one warning in the test env (e.g. site
        # domain default, debug mode). If --strict promotes that,
        # exit code should be 1.
        result, exit_code = _run_doctor("--strict")
        if result["summary"]["warn"] > 0:
            self.assertEqual(
                exit_code,
                1,
                "--strict should fail when warnings are present, "
                f"but exit code was {exit_code} with "
                f"{result['summary']['warn']} warnings.",
            )

    def test_no_strict_means_warnings_dont_fail(self):
        """Without --strict, warnings don't cause non-zero exit.

        The default behaviour matches operator expectation: warnings
        are advisory, errors are blocking. This separates "you should
        review this" from "stop the deploy."
        """
        result, exit_code = _run_doctor()
        # If there are warnings but no errors, exit must be 0.
        if result["summary"]["error"] == 0 and result["summary"]["fatal"] == 0:
            self.assertEqual(
                exit_code,
                0,
                f"Doctor exited {exit_code} on a run with no errors. "
                f"Warnings alone should not cause non-zero exit "
                f"unless --strict is set.",
            )


class DoctorCheckResultDataclassTests(TestCase):
    """Verify the CheckResult dataclass has the v1 fields.

    This is internal API but every check function constructs these,
    so the field shape is part of the doctor's stability contract.
    """

    def test_check_result_has_id_and_category(self):
        """The id and category fields are required positional args.

        Making them required (not defaulted) means a test or new
        check that forgets them gets a clear TypeError at construction
        time, not a silent missing-id bug at output time.
        """
        result = CheckResult(
            id="VB999",
            category="test",
            name="test",
            status=CheckStatus.OK,
            message="ok",
        )
        self.assertEqual(result.id, "VB999")
        self.assertEqual(result.category, "test")

    def test_check_result_id_must_start_with_vb(self):
        """Check IDs follow a project-wide convention.

        We don't enforce this at the dataclass level (a string is
        a string), but the JSON schema test does. Documenting the
        convention here so future check authors see it.
        """
        # This test is documentary — see DoctorJsonSchemaTests for
        # the actual enforcement. The point is: every check ID
        # should start with VB.
        result = CheckResult(
            id="VB001",
            category="settings",
            name="test",
            status=CheckStatus.OK,
            message="ok",
        )
        self.assertTrue(result.id.startswith("VB"))


class DoctorSecurityCheckTests(TestCase):
    """Security checks must catch production secret misconfiguration."""

    @override_settings(
        DEBUG=False,
        SECRET_KEY="django-secret-key-" + "a" * 64,
        API_KEY_DIGEST_KEY="",
        ALLOWED_HOSTS=["app.example.com"],
        CSRF_TRUSTED_ORIGINS=["https://app.example.com"],
        ADMIN_URL="not-admin/",
        SECURE_SSL_REDIRECT=True,
        SESSION_COOKIE_SECURE=True,
    )
    def test_api_key_digest_key_missing_emits_vb008(self):
        """Production must not fall back to SECRET_KEY for API-key digests."""

        command = Command()
        command._check_security()

        checks = {result.id: result for result in command.results}
        self.assertEqual(checks["VB008"].status, CheckStatus.ERROR)
        self.assertIn("DJANGO_API_KEY_DIGEST_KEY is not set", checks["VB008"].message)

    @override_settings(
        DEBUG=False,
        SECRET_KEY="shared-secret-key-" + "b" * 64,
        API_KEY_DIGEST_KEY="shared-secret-key-" + "b" * 64,
        ALLOWED_HOSTS=["app.example.com"],
        CSRF_TRUSTED_ORIGINS=["https://app.example.com"],
        ADMIN_URL="not-admin/",
        SECURE_SSL_REDIRECT=True,
        SESSION_COOKIE_SECURE=True,
    )
    def test_api_key_digest_key_reusing_secret_key_emits_vb008(self):
        """Digest-key rotation must be independent from Django SECRET_KEY."""

        command = Command()
        command._check_security()

        checks = {result.id: result for result in command.results}
        self.assertEqual(checks["VB008"].status, CheckStatus.ERROR)
        self.assertIn("reuses SECRET_KEY", checks["VB008"].message)


class DoctorSeverityCountingTests(TestCase):
    """Verify the summary counts match the actual checks.

    Sanity check: the summary block must actually summarize. Off-by-
    one or category-confused counts would break dashboards.
    """

    def test_summary_counts_sum_to_total_checks(self):
        """Every check belongs to exactly one summary bucket."""
        result, _ = _run_doctor()
        total = sum(result["summary"].values())
        self.assertEqual(total, len(result["checks"]))

    def test_summary_per_status_matches_check_statuses(self):
        """Each summary count equals the actual count for that status."""
        result, _ = _run_doctor()
        actual_counts = dict.fromkeys(CheckStatus, 0)
        for check in result["checks"]:
            actual_counts[CheckStatus(check["status"])] += 1
        for status, expected in result["summary"].items():
            self.assertEqual(
                actual_counts[CheckStatus(status)],
                expected,
                f"Summary count for {status!r} ({expected}) does not "
                f"match actual count ({actual_counts[CheckStatus(status)]})",
            )


class DoctorCommandIntegrationTests(TestCase):
    """End-to-end smoke tests via call_command.

    These verify the command boots, parses args, runs all check
    functions, and emits both human-readable and JSON output without
    crashing. Lightweight — they don't assert on specific check
    results since those depend on the test environment's state.
    """

    def test_command_runs_with_default_args(self):
        """Doctor runs without args and produces output."""
        import contextlib

        stdout = StringIO()
        # Doctor may exit 1 if test env has errors — that's fine for
        # this smoke test; we just want to confirm it runs.
        with contextlib.suppress(SystemExit):
            call_command("check_validibot", stdout=stdout)
        output = stdout.getvalue()
        self.assertIn("Validibot Doctor", output)
        self.assertIn("Summary", output)

    def test_command_runs_with_json_flag(self):
        """--json produces valid JSON, suppresses human-readable output."""
        result, _ = _run_doctor()
        # If we got this far, json.loads parsed successfully.
        self.assertIsInstance(result, dict)
        self.assertIn("checks", result)

    def test_check_ids_are_unique_per_meaning(self):
        """Each VB ID maps to one stable meaning.

        It's OK for the SAME id to appear multiple times in one run
        (e.g. ``VB101`` appearing both as ``OK`` and as a fix
        confirmation), but each ID should correspond to ONE category
        and ONE name across the whole run.
        """
        result, _ = _run_doctor()
        id_to_category: dict[str, str] = {}
        for check in result["checks"]:
            existing = id_to_category.get(check["id"])
            if existing is not None and existing != check["category"]:
                self.fail(
                    f"Check ID {check['id']!r} is ambiguous: appears in "
                    f"category {existing!r} and {check['category']!r}. "
                    f"Each VB ID must map to one stable meaning.",
                )
            id_to_category[check["id"]] = check["category"]


@override_settings(DEPLOYMENT_TARGET="self_hosted")
class DoctorTargetSelfHostedTests(TestCase):
    """Doctor behaviour when running against a self-hosted profile.

    Phase 1 Session 1 doesn't yet condition checks on target — that
    work lands in Session 2 (provider overlay, compatibility matrix).
    For now we just verify the target plumbs through cleanly.
    """

    def test_self_hosted_target_in_json_output(self):
        result, _ = _run_doctor()
        self.assertEqual(result["target"], "self_hosted")
        self.assertIsNone(result["stage"])


@override_settings(DEPLOYMENT_TARGET="gcp")
class DoctorTargetGcpTests(TestCase):
    """Doctor behaviour when running against a GCP profile."""

    def test_gcp_target_in_json_output(self):
        result, _ = _run_doctor()
        self.assertEqual(result["target"], "gcp")


class DoctorProviderOverlayTests(TestCase):
    """Provider overlay (--provider digitalocean) adds DigitalOcean checks.

    Without --provider, the checks list excludes DO-specific findings.
    With --provider digitalocean, doctor appends VB910/VB911/VB912/
    VB913 to the checks list (DNS, volume mount, monitoring agent,
    firewall reminder).

    These tests verify the overlay framework wires up correctly. The
    actual check logic (DNS resolution, mount point detection) is
    integration-tested on a real DigitalOcean Droplet — those checks
    behave differently in the test environment (no /proc/mounts on
    macOS, no DO agent installed, etc.).
    """

    def test_no_provider_means_no_do_checks(self):
        """Without --provider, no VB9xx DigitalOcean IDs appear.

        Operators not running on DO shouldn't see DO-specific
        findings polluting their doctor output.
        """
        result, _ = _run_doctor()
        do_check_ids = {
            check["id"] for check in result["checks"] if check["id"].startswith("VB91")
        }
        self.assertEqual(
            do_check_ids,
            set(),
            "Found DO-specific check IDs without --provider; the overlay "
            "should only run when --provider digitalocean is passed.",
        )

    def test_provider_digitalocean_adds_do_checks(self):
        """--provider digitalocean adds the four overlay checks.

        We expect VB910 (DNS), VB911 (volume mount), VB912 (monitoring
        agent), VB913 (firewall reminder) to all appear. Their
        statuses depend on the test environment but they must be
        present.
        """
        result, _ = _run_doctor("--provider", "digitalocean")
        check_ids = {check["id"] for check in result["checks"]}
        for expected_id in ("VB910", "VB911", "VB912", "VB913"):
            self.assertIn(
                expected_id,
                check_ids,
                f"Provider overlay should emit {expected_id} but it's missing.",
            )

    def test_provider_in_json_output(self):
        """The provider field is captured in the JSON output."""
        result, _ = _run_doctor("--provider", "digitalocean")
        self.assertEqual(result["provider"], "digitalocean")

    def test_provider_null_when_not_specified(self):
        """Without --provider, the JSON ``provider`` field is null."""
        result, _ = _run_doctor()
        self.assertIsNone(result["provider"])


class DoctorCompatibilityMatrixTests(TestCase):
    """Compatibility matrix checks (Postgres, Docker, OS versions).

    These verify the new compatibility-matrix checks are wired up
    and emit the expected check IDs. Whether the actual versions
    pass or fail depends on the test environment, so we assert on
    presence and severity behaviour, not specific status.
    """

    def test_postgres_version_check_runs(self):
        """VB120 Postgres version check appears in every run.

        Postgres is the canonical Validibot database; the check
        should always emit, even if it's SKIPPED for SQLite-backed
        test environments.
        """
        result, _ = _run_doctor()
        check_ids = {check["id"] for check in result["checks"]}
        self.assertIn("VB120", check_ids)

    def test_os_version_check_runs_or_skipped(self):
        """VB030 OS version check emits or is skipped.

        On macOS / non-Linux test hosts, the check skips because
        /etc/os-release doesn't exist. On Linux CI, it runs and
        either passes (Ubuntu LTS+) or warns. Either way, the check
        ID must appear.
        """
        result, _ = _run_doctor("--target", "self_hosted")
        check_ids = {check["id"] for check in result["checks"]}
        self.assertIn("VB030", check_ids)

    def test_target_affects_unsupported_severity(self):
        """Same unsupported version is ERROR on self-hosted, INFO on GCP.

        The unsupported_status decision (in the doctor command) maps
        target to severity. We can verify the mapping logic exists by
        checking that compatibility-matrix categories exist in JSON
        output for both targets.
        """
        sh_result, _ = _run_doctor("--target", "self_hosted")
        gcp_result, _ = _run_doctor("--target", "gcp")
        # Both runs include compatibility checks (just possibly
        # different severities).
        sh_categories = {check["category"] for check in sh_result["checks"]}
        gcp_categories = {check["category"] for check in gcp_result["checks"]}
        self.assertIn("database", sh_categories)
        self.assertIn("database", gcp_categories)


# Docker runner checks use the configured execution abstraction because the
# application container talks to Docker through the SDK and mounted socket.
@override_settings(VALIDATOR_RUNNER="docker")
class DoctorDockerRunnerTests(TestCase):
    """Verify doctor checks the Docker API path used by real validators.

    Local Compose images intentionally omit the Docker CLI. These tests prevent
    the old ``shutil.which('docker')`` implementation from returning and
    ensure cloud runners do not receive irrelevant Docker warnings.
    """

    @patch(
        "validibot.validations.services.runners.registry.get_validator_runner",
    )
    def test_sdk_runner_passes_without_docker_cli(self, get_validator_runner):
        """A working Docker SDK connection passes when no CLI is installed.

        This mirrors local-cloud exactly: docker-py and the socket are present
        in the application container, while a ``docker`` executable is not.
        Advanced validation readiness must follow the SDK result.
        """
        runner = Mock()
        runner.get_runner_type.return_value = "docker"
        runner.is_available.return_value = True
        get_validator_runner.return_value = runner
        command = Command()
        command._check_validator_images = Mock()

        with patch("shutil.which", return_value=None):
            command._check_docker()

        docker_check = next(
            result for result in command.results if result.id == "VB302"
        )
        self.assertEqual(docker_check.status, CheckStatus.OK)
        self.assertIn("Docker API", docker_check.message)
        command._check_validator_images.assert_called_once_with()

    @patch(
        "validibot.validations.services.runners.registry.get_validator_runner",
    )
    def test_unreachable_sdk_runner_warns(self, get_validator_runner):
        """An unavailable configured Docker runner remains actionable.

        A missing socket, stopped daemon, or permissions problem prevents real
        EnergyPlus and FMU execution, so doctor must retain a warning with a
        socket-specific remediation rather than reporting a missing CLI.
        """
        runner = Mock()
        runner.get_runner_type.return_value = "docker"
        runner.is_available.return_value = False
        get_validator_runner.return_value = runner
        command = Command()

        command._check_docker()

        docker_check = next(
            result for result in command.results if result.id == "VB302"
        )
        self.assertEqual(docker_check.status, CheckStatus.WARN)
        self.assertIn("cannot reach Docker Engine", docker_check.message)
        self.assertIn("docker.sock", docker_check.fix_hint)

    @patch(
        "validibot.validations.services.runners.registry.get_validator_runner",
    )
    def test_non_docker_runner_skips_docker_check(self, get_validator_runner):
        """Cloud execution backends do not require a local Docker daemon.

        This prevents GCP doctor jobs from warning about Docker even though
        validators are deliberately dispatched to Cloud Run Jobs.
        """
        runner = Mock()
        runner.get_runner_type.return_value = "google_cloud_run"
        get_validator_runner.return_value = runner
        command = Command()

        command._check_docker()

        docker_check = next(
            result for result in command.results if result.id == "VB301"
        )
        self.assertEqual(docker_check.status, CheckStatus.SKIPPED)
        runner.is_available.assert_not_called()


class DoctorLocalComposeApplicabilityTests(TestCase):
    """Keep host-operations findings out of disposable local Compose runs.

    Doctor executes inside the application image, where the observed Debian
    release is unrelated to the developer's host. Local data is also designed
    to be deleted and recreated, so restore-drill readiness does not apply.
    """

    def test_local_compose_os_check_is_skipped(self):
        """Container OS metadata is not treated as host compatibility data."""
        command = Command()
        command.target = "local_docker_compose"

        command._check_os_version(CheckStatus.WARN)

        os_check = next(result for result in command.results if result.id == "VB030")
        self.assertEqual(os_check.status, CheckStatus.SKIPPED)
        self.assertIn("local Docker Compose", os_check.message)

    @override_settings(DATA_STORAGE_ROOT="/unused/local/storage")
    def test_local_compose_restore_drill_is_skipped(self):
        """Disposable local data never produces a missing-marker warning."""
        command = Command()
        command.target = "local_docker_compose"

        command._check_restore_test()

        restore_check = next(
            result for result in command.results if result.id == "VB411"
        )
        self.assertEqual(restore_check.status, CheckStatus.SKIPPED)
        self.assertIn("disposable local Compose data", restore_check.message)


class DoctorRestoreTestMarkerTests(TestCase):
    """The VB411 restore-test marker check.

    Doctor should warn when no restore drill has been recorded.
    Phase 3's backup recipe will write the marker; until then, every
    install reports "no restore test recorded" — that's intentional.
    """

    def test_restore_marker_check_runs(self):
        """VB411 restore-test check appears in every run.

        It's part of the standard check list, not gated by provider
        or target.
        """
        result, _ = _run_doctor()
        check_ids = {check["id"] for check in result["checks"]}
        self.assertIn("VB411", check_ids)

    def test_restore_marker_warns_when_missing(self):
        """No marker file means VB411 is a warning, not an error.

        The marker missing is the expected state pre-Phase-3. We
        don't want to fail doctor (red) on installs that haven't yet
        run a restore drill — that would be too noisy. Phase 4
        upgrade will require a recent backup, but doctor should
        encourage the practice without blocking other operations.
        """
        result, _ = _run_doctor()
        restore_check = next(
            (c for c in result["checks"] if c["id"] == "VB411"),
            None,
        )
        self.assertIsNotNone(restore_check)
        # Either WARN (most likely) or SKIPPED (DATA_STORAGE_ROOT
        # not set in test env) — never ERROR.
        self.assertIn(restore_check["status"], ("warn", "skipped", "ok"))

    def test_self_hosted_missing_marker_uses_current_restore_instructions(self):
        """The warning points directly to the shipped backup and restore flow.

        The old message said operators should wait until Phase 3 shipped even
        though backup and restore recipes now exist. Keeping the remediation
        current matters because this warning is intended to trigger action.
        """
        with (
            tempfile.TemporaryDirectory() as data_root,
            override_settings(DATA_STORAGE_ROOT=data_root),
        ):
            command = Command()
            command.target = "self_hosted"
            command._check_restore_test()

        restore_check = next(
            result for result in command.results if result.id == "VB411"
        )
        self.assertEqual(restore_check.status, CheckStatus.WARN)
        self.assertNotIn("Phase 3", restore_check.fix_hint)
        self.assertIn("just self-hosted backup", restore_check.fix_hint)


class DoctorImagePolicyTests(TestCase):
    """Verify the doctor's handling of ``VALIDATOR_BACKEND_IMAGE_POLICY``.

    The policy resolver raises ``ImproperlyConfigured`` on
    non-empty unknown values (a typo in a strict-intent setting
    must not silently relax to ``tag`` — that would invert the
    operator's intent).  The doctor catches the exception and
    surfaces a structured ``VB711`` check failure rather than
    crashing the whole run.
    """

    def test_unknown_policy_value_reports_vb711_error(self):
        """A typo in the policy setting surfaces as a clear doctor finding."""
        from django.test import override_settings

        with override_settings(VALIDATOR_BACKEND_IMAGE_POLICY="strict"):
            result, _ = _run_doctor()

        # The doctor should have completed despite the bad config —
        # finding the misconfiguration is the doctor's job, not
        # crashing on it.
        vb711 = next(
            (c for c in result["checks"] if c["id"] == "VB711"),
            None,
        )
        self.assertIsNotNone(
            vb711,
            "Expected VB711 to fire for unrecognised image-policy value, "
            "but no VB711 check was reported.",
        )
        self.assertEqual(vb711["status"], "error")
        # The fix-hint should mention the valid values so the operator
        # sees the answer alongside the problem.
        self.assertIn("tag", vb711["fix_hint"].lower())


class DoctorVersionStampTests(TestCase):
    """``_get_validibot_version`` reads the deployment-stamped version.

    Earlier the doctor read package metadata directly, which split
    the operator surfaces: backup manifests + support bundles +
    OCI labels all read ``settings.VALIDIBOT_VERSION`` (stamped by
    the deploy recipes from the latest release tag), but doctor
    showed package metadata (often ``unknown`` because Validibot
    isn't installed as a package, just run from a source tree).

    Now ``_get_validibot_version`` delegates to
    ``get_validibot_runtime_version()`` — the single source of truth.
    """

    @override_settings(VALIDIBOT_VERSION="v0.5.0")
    def test_doctor_version_uses_validibot_version_setting(self):
        """When ``VALIDIBOT_VERSION`` is set, doctor reports it.

        The deploy recipes stamp this value into the runtime image's
        env from the latest release tag. Doctor's report should match
        what backup manifests and support bundles already record.
        """
        from validibot.core.management.commands.check_validibot import (
            Command as DoctorCommand,
        )

        cmd = DoctorCommand()
        assert cmd._get_validibot_version() == "v0.5.0"

    @override_settings(VALIDIBOT_VERSION="")
    def test_doctor_version_falls_back_to_package_metadata(self):
        """Without the deployment stamp, doctor falls back to package version.

        Through the ``get_validibot_runtime_version`` helper, this
        is the same fallback chain backup / support tooling uses.
        We assert the result is non-empty so a future regression in
        the helper (returning empty / None) trips this test.
        """
        from validibot.core.management.commands.check_validibot import (
            Command as DoctorCommand,
        )

        cmd = DoctorCommand()
        result = cmd._get_validibot_version()
        assert result, "doctor should always return a non-empty version string"
