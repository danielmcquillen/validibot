"""Tests for the ``smoke_test`` Django management command.

The smoke test is the cross-target end-to-end verification that
``just self-hosted smoke-test`` and ``just gcp smoke-test`` shell
into. These tests pin the contract:

1. **Idempotent fixtures** — re-running the command does not
   duplicate the demo org / user / workflow / step. A fresh
   ValidationRun is created on each invocation, but the surrounding
   demo data is reused via ``get_or_create``.
2. **Demo-data marking** — every fixture uses the documented
   ``smoke-test-`` slug prefix and ``[Demo]`` name suffix, so the
   data is unambiguous in the UI and admin.
3. **Required system validator** — the command fails fatally with
   a clear fix-hint when the JSON Schema system validator hasn't
   been created (i.e. ``setup_validibot`` hasn't run).
4. **Outcome distinction** — SUCCEEDED is OK, VALIDATION_FAILED is
   ERROR with a different message, FAILED is ERROR with another
   different message. The taxonomy is part of the contract.
5. **JSON output schema** — the v1 schema's top-level keys and
   per-result keys are stable.
6. **Exit code semantics** — the command exits 0 on pass and 1 on
   any blocking status. CI / scripts can branch on the exit code.

Why these tests don't actually exercise the real worker
=======================================================

The end-to-end "real worker picks up the job" path requires Celery
plus Redis plus a Compose stack — too heavy for unit tests. Test
mode runs synchronously inline (``test_dispatcher.py``), which IS
the actual code path under ``DEPLOYMENT_TARGET=test``, so we get
genuine end-to-end coverage of the in-process flow without
container infrastructure.

For the genuine "queue + worker" smoke test, the operator-level
walkthrough in ``docs/operations/self-hosting/smoke-test.md`` is
the integration boundary — those steps are exercised by the manual
release checklist in the ADR.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command

from validibot.core.management.commands.smoke_test import DEMO_ORG_SLUG
from validibot.core.management.commands.smoke_test import DEMO_USERNAME
from validibot.core.management.commands.smoke_test import DEMO_WORKFLOW_SLUG
from validibot.core.management.commands.smoke_test import SMOKE_TEST_SCHEMA_VERSION
from validibot.users.models import Organization
from validibot.users.models import User
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import Validator
from validibot.workflows.models import Workflow

pytestmark = pytest.mark.django_db


@pytest.fixture
def system_json_schema_validator():
    """Provision the system JSON Schema validator the smoke test depends on.

    ``setup_validibot`` creates this in real deployments; for unit
    tests we create it explicitly so each test starts from a clean,
    deterministic state. The smoke test's ``--target`` defaults to
    ``test`` here because ``DJANGO_SETTINGS_MODULE=config.settings.test``
    sets ``DEPLOYMENT_TARGET=test`` — runs go through the inline
    test dispatcher, which means the smoke test completes
    synchronously without needing Celery or Redis.
    """
    return Validator.objects.create(
        slug="json-schema-system",
        name="JSON Schema",
        validation_type=ValidationType.JSON_SCHEMA,
        version="1",
        is_system=True,
        org=None,
    )


# ──────────────────────────────────────────────────────────────────────
# Idempotent fixtures
# ──────────────────────────────────────────────────────────────────────


class TestIdempotentFixtures:
    """Re-running the command reuses fixtures rather than duplicating them."""

    def test_first_run_creates_demo_data(self, system_json_schema_validator):
        """The first invocation creates demo org / user / workflow."""
        call_command("smoke_test", "--json", stdout=StringIO())

        assert Organization.objects.filter(slug=DEMO_ORG_SLUG).count() == 1
        assert User.objects.filter(username=DEMO_USERNAME).count() == 1
        assert Workflow.objects.filter(slug=DEMO_WORKFLOW_SLUG).count() == 1
        assert Workflow.objects.get(slug=DEMO_WORKFLOW_SLUG).steps.count() == 1

    def test_second_run_does_not_duplicate_fixtures(
        self,
        system_json_schema_validator,
    ):
        """Running twice keeps exactly one of each demo object.

        This is the core idempotency contract: a smoke test is a
        sanity check operators run frequently (after install, after
        deploy, in CI), so it must NEVER pile up demo data.
        """
        call_command("smoke_test", "--json", stdout=StringIO())
        call_command("smoke_test", "--json", stdout=StringIO())

        assert Organization.objects.filter(slug=DEMO_ORG_SLUG).count() == 1
        assert User.objects.filter(username=DEMO_USERNAME).count() == 1
        assert Workflow.objects.filter(slug=DEMO_WORKFLOW_SLUG).count() == 1
        # Each workflow keeps its single step — re-runs don't add steps either.
        assert Workflow.objects.get(slug=DEMO_WORKFLOW_SLUG).steps.count() == 1


# ──────────────────────────────────────────────────────────────────────
# Demo-data marking
# ──────────────────────────────────────────────────────────────────────


class TestDemoDataMarking:
    """Demo data uses documented identifiers so it's unambiguous in the UI."""

    def test_org_uses_smoke_test_prefix(self, system_json_schema_validator):
        call_command("smoke_test", "--json", stdout=StringIO())
        org = Organization.objects.get(slug=DEMO_ORG_SLUG)
        # The slug carries the prefix; the human-readable name carries
        # the [Demo] suffix. Both are searchable.
        assert org.slug.startswith("smoke-test-")
        assert "[Demo]" in org.name

    def test_workflow_uses_smoke_test_prefix(
        self,
        system_json_schema_validator,
    ):
        call_command("smoke_test", "--json", stdout=StringIO())
        workflow = Workflow.objects.get(slug=DEMO_WORKFLOW_SLUG)
        assert workflow.slug.startswith("smoke-test-")
        assert "[Demo]" in workflow.name

    def test_user_has_unusable_password(self, system_json_schema_validator):
        """The smoke-test user must not be a usable interactive account.

        ``set_unusable_password()`` writes an unsalted hash that no
        password can ever match. This prevents the demo user from
        becoming a vector if an operator forgets to clean it up
        before going to production.
        """
        call_command("smoke_test", "--json", stdout=StringIO())
        user = User.objects.get(username=DEMO_USERNAME)
        assert not user.has_usable_password()


# ──────────────────────────────────────────────────────────────────────
# Missing-fixture failure mode
# ──────────────────────────────────────────────────────────────────────


class TestMissingSystemValidator:
    """The command fails fatally when ``setup_validibot`` hasn't run."""

    def test_no_validator_produces_fatal_with_fix_hint(self):
        """ST001 reports FATAL pointing operators at setup_validibot.

        This is the most common failure mode for a fresh install
        that skipped the bootstrap step. The fix-hint must name the
        actual command operators should run, not a generic
        "configuration error" message.
        """
        out = StringIO()
        # No system validator created — the smoke test should fail
        # with a clear pointer to the setup command.
        with pytest.raises(SystemExit):
            call_command("smoke_test", "--json", stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["passed"] is False
        st001 = next(r for r in payload["results"] if r["id"] == "ST001")
        assert st001["status"] == "fatal"
        assert "setup_validibot" in (st001["fix_hint"] or "")


# ──────────────────────────────────────────────────────────────────────
# Outcome taxonomy
# ──────────────────────────────────────────────────────────────────────


class TestOutcomeTaxonomy:
    """ST005 reports distinct messages for SUCCEEDED / VALIDATION_FAILED / FAILED."""

    def test_succeeded_run_reports_ok(self, system_json_schema_validator):
        """A passing smoke test is the happy-path contract.

        In test mode the inline dispatcher actually runs the
        validation. With the demo schema + matching demo payload,
        the run reaches SUCCEEDED and ST005 is OK.
        """
        out = StringIO()
        call_command("smoke_test", "--json", stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["passed"] is True
        st005 = next(r for r in payload["results"] if r["id"] == "ST005")
        assert st005["status"] == "ok"
        assert "succeeded" in st005["message"].lower()

    def test_failed_run_with_findings_is_distinct_from_system_failure(
        self,
        system_json_schema_validator,
    ):
        """A FAILED run with findings reports the validator-level failure.

        The status enum has only one failure terminal (``FAILED``).
        ST005 splits FAILED into two distinct operator-facing
        modes by inspecting ``total_findings``:

        - findings > 0  → "validator reported issues on the demo
          payload" (validator-level failure)
        - findings == 0 → "system error before findings could be
          recorded" (worker / dispatcher / config issue)

        We force the findings>0 case by tampering with the demo
        ruleset's schema so the demo payload no longer satisfies it.
        ST005 should report ERROR with a message that names "findings"
        — not the generic "system error" wording.
        """
        # First run creates the ruleset; second run reuses it.
        call_command("smoke_test", "--json", stdout=StringIO())

        # Tamper the demo ruleset so the next run produces findings.
        rs = Ruleset.objects.filter(
            ruleset_type=RulesetType.JSON_SCHEMA,
            name__icontains="Smoke Test",
        ).first()
        assert rs is not None, "Demo ruleset should exist after first run"
        rs.rules_text = json.dumps(
            {
                "type": "object",
                "properties": {"smoke_test": {"type": "string", "const": "NOT_OK"}},
                "required": ["smoke_test"],
            },
        )
        rs.save()

        out = StringIO()
        with pytest.raises(SystemExit):
            call_command("smoke_test", "--json", stdout=out)

        payload = json.loads(out.getvalue())
        assert payload["passed"] is False
        st005 = next(r for r in payload["results"] if r["id"] == "ST005")
        assert st005["status"] == "error"
        # The findings-mode message must surface a finding count so
        # the operator can distinguish it from the system-error path.
        assert "finding" in st005["message"].lower(), (
            f"Expected ST005 message to mention findings, got: {st005['message']!r}"
        )


# ──────────────────────────────────────────────────────────────────────
# JSON schema stability
# ──────────────────────────────────────────────────────────────────────


class TestJsonSchemaStability:
    """Top-level keys + per-result keys are operator-readable contracts."""

    def test_top_level_keys(self, system_json_schema_validator):
        out = StringIO()
        call_command("smoke_test", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        # Renaming any of these breaks dashboard / CI integrations
        # that consume the smoke-test report. Adding new keys is
        # additive and stays v1.
        assert set(payload.keys()) >= {
            "schema_version",
            "generated_at",
            "target",
            "stage",
            "passed",
            "results",
        }
        assert payload["schema_version"] == SMOKE_TEST_SCHEMA_VERSION

    def test_result_keys(self, system_json_schema_validator):
        out = StringIO()
        call_command("smoke_test", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        for result in payload["results"]:
            assert {
                "id",
                "category",
                "name",
                "status",
                "message",
                "details",
                "fix_hint",
            } == set(result.keys())

    def test_signed_credential_check_skipped_on_community(
        self,
        system_json_schema_validator,
    ):
        """ST006 is SKIPPED on community deployments — never silently passed.

        ADR section 7: "For self-hosted without Pro/signing, the
        check reports SKIPPED, not silently passed." A SKIPPED
        result keeps the JSON output's shape consistent across
        community / Pro editions while making it clear the check
        didn't run.
        """
        out = StringIO()
        call_command("smoke_test", "--json", stdout=out)
        payload = json.loads(out.getvalue())
        st006 = next(r for r in payload["results"] if r["id"] == "ST006")
        assert st006["status"] == "skipped"


# ──────────────────────────────────────────────────────────────────────
# Exit code semantics
# ──────────────────────────────────────────────────────────────────────


class TestExitCodeSemantics:
    """Exit 0 on pass, 1 on any blocking status. CI relies on this."""

    def test_pass_does_not_raise(self, system_json_schema_validator):
        # A passing run completes without SystemExit.
        call_command("smoke_test", "--json", stdout=StringIO())

    def test_fatal_raises_system_exit(self):
        """Missing validator → fatal → SystemExit(1)."""
        with pytest.raises(SystemExit) as exc:
            call_command("smoke_test", "--json", stdout=StringIO())
        assert exc.value.code == 1
