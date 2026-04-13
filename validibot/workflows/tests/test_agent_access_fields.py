"""
Tests for agent access fields on the Workflow model.

These tests verify the ``agent_access_enabled`` visibility flag, the
``agent_billing_mode`` enum, and the ``clean()`` validation rules that
govern their interaction:

- ``agent_access_enabled`` and ``agent_billing_mode`` are independent.
  Any combination of the two is valid except x402 billing without a price.
- The x402 billing mode requires a non-zero ``agent_price_cents``.

The two fields were decoupled in April 2026 (ADR-2026-03-03 Phase 3a
revision).  Previously, enabling agent access required x402 billing;
now ``agent_access_enabled=True`` + ``AUTHOR_PAYS`` is a valid state
that exposes the workflow to authenticated agents only.
"""

import pytest
from django.core.exceptions import ValidationError

from validibot.submissions.constants import SubmissionFileType
from validibot.workflows.constants import AgentBillingMode
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


# ── Billing mode enum ────────────────────────────────────────────────
# The enum values determine how agent invocations are billed.
# AUTHOR_PAYS uses the workflow author's plan quota (authenticated
# agents only).  AGENT_PAYS_X402 requires per-call x402 micropayments.


class TestAgentBillingModeEnum:
    """Verify that the billing mode enum has the expected values.

    These tests act as a safety net: if someone adds a new billing mode
    without updating the ADR and the clean() validation, the test suite
    will catch the mismatch.
    """

    def test_author_pays_is_default(self):
        """AUTHOR_PAYS should be the default — the workflow author's
        plan quota covers agent usage for authenticated agents."""
        wf = WorkflowFactory.build()
        assert wf.agent_billing_mode == AgentBillingMode.AUTHOR_PAYS

    def test_x402_is_available(self):
        """AGENT_PAYS_X402 should be a valid choice for enabling
        anonymous micropayment access."""
        assert AgentBillingMode.AGENT_PAYS_X402 in AgentBillingMode.values

    def test_acp_removed(self):
        """AGENT_PAYS_ACP should no longer be in the active choices.
        It was removed in the April 2026 ADR revision because Stripe
        ACP's consumer deployment was scaled back."""
        assert "agent_pays_acp" not in AgentBillingMode.values


# ── Agent access flag default ───────────────────────────────────────
# By default, workflows are not exposed to agents via MCP.


class TestAgentAccessEnabledDefault:
    """Verify that the agent access flag defaults to False."""

    def test_defaults_to_false(self):
        """New workflows should not be visible to agents by default.
        The author must explicitly opt in via the superuser form."""
        wf = WorkflowFactory.build()
        assert wf.agent_access_enabled is False


# ── clean() validation: decoupled fields ────────────────────────────
# agent_access_enabled and agent_billing_mode are independent — any
# combination is valid.  The only constraint is that x402 billing
# requires a non-zero price.


class TestAgentAccessBillingDecoupled:
    """Verify that agent_access_enabled and agent_billing_mode are
    independent: enabling access does not require any particular
    billing mode, and vice versa."""

    def test_enabled_with_author_pays_is_valid(self):
        """agent_access_enabled=True + AUTHOR_PAYS is the authenticated-
        only MCP access use case.  The author's plan quota covers agent
        usage — no x402 payment required."""
        wf = WorkflowFactory.build(
            agent_access_enabled=True,
            agent_billing_mode=AgentBillingMode.AUTHOR_PAYS,
        )
        wf.clean()  # should not raise

    def test_enabled_with_x402_and_price_is_valid(self):
        """agent_access_enabled=True + AGENT_PAYS_X402 + a price is the
        full anonymous marketplace configuration."""
        wf = WorkflowFactory.build(
            agent_access_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
        )
        wf.clean()  # should not raise

    def test_disabled_with_author_pays_is_valid(self):
        """The default state: access disabled, AUTHOR_PAYS.  Nothing
        exposed, nothing to validate."""
        wf = WorkflowFactory.build(
            agent_access_enabled=False,
            agent_billing_mode=AgentBillingMode.AUTHOR_PAYS,
        )
        wf.clean()  # should not raise

    def test_disabled_with_x402_configured_is_valid(self):
        """A workflow can have x402 billing configured but not be exposed
        yet.  This allows staging the configuration before publishing."""
        wf = WorkflowFactory.build(
            agent_access_enabled=False,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=25,
        )
        wf.clean()  # should not raise


# ── clean() validation: x402 requires a price ──────────────────────
# This constraint applies regardless of the agent_access_enabled flag
# because a misconfigured price should be caught early.


class TestX402RequiresPrice:
    """Verify that selecting x402 billing without a valid price is
    rejected by clean().  This rule is independent of the access flag."""

    def test_x402_without_price_raises(self):
        """Setting the billing mode to x402 without a price is an error.
        Agents need to know how much to pay."""
        wf = WorkflowFactory.build(
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=None,
        )
        with pytest.raises(ValidationError) as exc_info:
            wf.clean()
        assert "agent_price_cents" in exc_info.value.message_dict

    def test_x402_with_zero_price_raises(self):
        """A zero price is not meaningful for x402 — the agent would be
        asked to pay $0.00, which wastes a blockchain transaction for
        nothing.  Require a positive price."""
        wf = WorkflowFactory.build(
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=0,
        )
        with pytest.raises(ValidationError) as exc_info:
            wf.clean()
        assert "agent_price_cents" in exc_info.value.message_dict


# ── clean() validation: x402 requires DO_NOT_STORE retention ────────
# This is a privacy invariant, not a pricing one: x402 enables anonymous
# per-call access, and storing agent submissions after the call would
# break the anonymity guarantee.  Enforced on the model so the rule
# applies to every write path (API, admin, form, programmatic save).


class TestX402RequiresDoNotStore:
    """Verify that x402 billing mode requires DO_NOT_STORE retention.

    The check matters because authors could otherwise accidentally
    configure an anonymous-pay workflow that silently retains submissions,
    which would violate the privacy promise of x402."""

    def test_x402_with_store_7_days_raises(self):
        """x402 paired with any non-DO_NOT_STORE retention should be
        rejected.  The default retention (STORE_7_DAYS) is the most
        likely accidental combination."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            data_retention=SubmissionRetention.STORE_7_DAYS,
        )
        with pytest.raises(ValidationError) as exc_info:
            wf.clean()
        assert "data_retention" in exc_info.value.message_dict

    def test_x402_with_store_permanently_raises(self):
        """Permanent storage is the most privacy-hostile pairing with
        x402 — explicitly reject it."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            data_retention=SubmissionRetention.STORE_PERMANENTLY,
        )
        with pytest.raises(ValidationError) as exc_info:
            wf.clean()
        assert "data_retention" in exc_info.value.message_dict

    def test_x402_with_do_not_store_is_valid(self):
        """The only retention allowed alongside x402: immediate deletion
        after validation.  This preserves the privacy model."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            data_retention=SubmissionRetention.DO_NOT_STORE,
        )
        wf.clean()  # should not raise

    def test_author_pays_with_any_retention_is_valid(self):
        """The retention rule only applies to x402.  AUTHOR_PAYS has no
        anonymity guarantee to break, so any retention policy is fine."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            agent_billing_mode=AgentBillingMode.AUTHOR_PAYS,
            data_retention=SubmissionRetention.STORE_PERMANENTLY,
        )
        wf.clean()  # should not raise


# ── Source resolution ────────────────────────────────────────────────
# The _resolve_api_source helper normalises the X-Validibot-Source
# header to uppercase and validates it against the allowed enum values.


class TestResolveApiSource:
    """Verify the source resolution logic in views_launch_helpers.

    Source is an analytics tag, not a security boundary. The main
    requirement is that it normalises to one of the known uppercase
    values (LAUNCH_PAGE, API, MCP) and rejects garbage.
    """

    def test_mcp_uppercase(self):
        """An uppercase 'MCP' header should resolve to the MCP source."""
        from django.test import RequestFactory

        from validibot.validations.constants import ValidationRunSource
        from validibot.workflows.views_launch_helpers import _resolve_api_source

        rf = RequestFactory()
        request = rf.get("/", HTTP_X_VALIDIBOT_SOURCE="MCP")
        assert _resolve_api_source(request) == ValidationRunSource.MCP

    def test_mcp_lowercase(self):
        """A lowercase 'mcp' header should also resolve to MCP after
        uppercase normalisation."""
        from django.test import RequestFactory

        from validibot.validations.constants import ValidationRunSource
        from validibot.workflows.views_launch_helpers import _resolve_api_source

        rf = RequestFactory()
        request = rf.get("/", HTTP_X_VALIDIBOT_SOURCE="mcp")
        assert _resolve_api_source(request) == ValidationRunSource.MCP

    def test_invalid_value_defaults_to_api(self):
        """An unrecognised header value should default to API, not crash."""
        from django.test import RequestFactory

        from validibot.validations.constants import ValidationRunSource
        from validibot.workflows.views_launch_helpers import _resolve_api_source

        rf = RequestFactory()
        request = rf.get("/", HTTP_X_VALIDIBOT_SOURCE="garbage")
        assert _resolve_api_source(request) == ValidationRunSource.API

    def test_missing_header_defaults_to_api(self):
        """No header at all should default to API."""
        from django.test import RequestFactory

        from validibot.validations.constants import ValidationRunSource
        from validibot.workflows.views_launch_helpers import _resolve_api_source

        rf = RequestFactory()
        request = rf.get("/")
        assert _resolve_api_source(request) == ValidationRunSource.API

    def test_launch_page_rejected_from_api(self):
        """API callers cannot claim to be the launch page — that source
        is reserved for the web form."""
        from django.test import RequestFactory

        from validibot.validations.constants import ValidationRunSource
        from validibot.workflows.views_launch_helpers import _resolve_api_source

        rf = RequestFactory()
        request = rf.get("/", HTTP_X_VALIDIBOT_SOURCE="LAUNCH_PAGE")
        assert _resolve_api_source(request) == ValidationRunSource.API


# ── Form: superuser-only agent fields ───────────────────────────────
# The WorkflowForm conditionally adds agent access fields when the
# user is a superuser.  Non-superusers should not see or be able to
# submit these fields.


class TestWorkflowFormAgentFields:
    """Verify that agent access fields appear only for superusers."""

    def _make_user(self, *, is_superuser: bool = False):
        """Create a minimal user-like object for form instantiation."""
        from unittest.mock import Mock

        user = Mock()
        user.is_superuser = is_superuser
        user.is_authenticated = True
        user.get_current_org.return_value = None
        return user

    def test_non_superuser_form_excludes_agent_fields(self):
        """Regular users should not see agent access fields in the
        workflow form.  These fields are platform-operator controls."""
        from validibot.workflows.forms import WorkflowForm

        form = WorkflowForm(user=self._make_user(is_superuser=False))
        assert "agent_access_enabled" not in form.fields
        assert "agent_billing_mode" not in form.fields
        assert "agent_price_cents" not in form.fields
        assert "agent_max_launches_per_hour" not in form.fields

    def test_superuser_form_includes_agent_fields(self):
        """Superusers should see all four agent access fields in the
        workflow form."""
        from validibot.workflows.forms import WorkflowForm

        form = WorkflowForm(user=self._make_user(is_superuser=True))
        assert "agent_access_enabled" in form.fields
        assert "agent_billing_mode" in form.fields
        assert "agent_price_cents" in form.fields
        assert "agent_max_launches_per_hour" in form.fields

    def test_form_rejects_x402_without_do_not_store(self):
        """A superuser-submitted form that pairs x402 billing with a
        non-DO_NOT_STORE retention should fail validation with the
        error attached to the ``data_retention`` field.

        This mirrors the model-level rule but surfaces the error on
        the form field so the UI can highlight the right control."""
        from validibot.submissions.constants import SubmissionRetention
        from validibot.users.models import ensure_default_project
        from validibot.users.tests.factories import MembershipFactory
        from validibot.users.tests.factories import OrganizationFactory
        from validibot.users.tests.factories import UserFactory
        from validibot.workflows.forms import WorkflowForm

        org = OrganizationFactory()
        user = UserFactory(is_superuser=True)
        MembershipFactory(user=user, org=org, is_active=True)
        user.set_current_org(org)
        default_project = ensure_default_project(org)

        form = WorkflowForm(
            data={
                "name": "Anonymous agent workflow",
                "slug": "anon-agent",
                "project": str(default_project.pk),
                "allowed_file_types": [SubmissionFileType.JSON],
                "data_retention": SubmissionRetention.STORE_7_DAYS,
                "output_retention": "STORE_30_DAYS",
                "version": "1.0",
                "is_active": "on",
                "agent_access_enabled": "on",
                "agent_billing_mode": AgentBillingMode.AGENT_PAYS_X402,
                "agent_price_cents": "10",
            },
            user=user,
        )
        assert not form.is_valid()
        assert "data_retention" in form.errors

    def test_form_accepts_x402_with_do_not_store(self):
        """The valid combination: x402 billing + DO_NOT_STORE retention."""
        from validibot.submissions.constants import SubmissionRetention
        from validibot.users.models import ensure_default_project
        from validibot.users.tests.factories import MembershipFactory
        from validibot.users.tests.factories import OrganizationFactory
        from validibot.users.tests.factories import UserFactory
        from validibot.workflows.forms import WorkflowForm

        org = OrganizationFactory()
        user = UserFactory(is_superuser=True)
        MembershipFactory(user=user, org=org, is_active=True)
        user.set_current_org(org)
        default_project = ensure_default_project(org)

        form = WorkflowForm(
            data={
                "name": "Anonymous agent workflow",
                "slug": "anon-agent",
                "project": str(default_project.pk),
                "allowed_file_types": [SubmissionFileType.JSON],
                "data_retention": SubmissionRetention.DO_NOT_STORE,
                "output_retention": "STORE_30_DAYS",
                "version": "1.0",
                "is_active": "on",
                "agent_access_enabled": "on",
                "agent_billing_mode": AgentBillingMode.AGENT_PAYS_X402,
                "agent_price_cents": "10",
            },
            user=user,
        )
        assert form.is_valid(), form.errors
