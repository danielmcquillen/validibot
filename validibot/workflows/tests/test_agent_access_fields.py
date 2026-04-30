"""
Tests for agent access fields on the Workflow model.

These tests verify the two-level agent visibility system:

- ``agent_access_enabled`` — master switch for all agent access via MCP.
  When True, authenticated agents in the workflow's org can discover and
  invoke it.
- ``agent_public_discovery`` — whether the workflow appears on the cross-org
  public catalog for external agent discovery.  Enabling this automatically
  forces ``agent_billing_mode=AGENT_PAYS_X402`` (the cascade is enforced
  in ``clean()`` so it applies to every write path).
- ``agent_billing_mode`` — who pays: AUTHOR_PAYS (plan quota) or
  AGENT_PAYS_X402 (per-call micropayment).

Constraint hierarchy (enforced via cascades, not validation errors):
- ``agent_public_discovery=True`` requires ``agent_access_enabled=True``
- ``agent_public_discovery=True`` forces ``agent_billing_mode=AGENT_PAYS_X402``
- ``agent_access_enabled=False`` forces ``agent_public_discovery=False``

History: the two fields were decoupled in April 2026 and the public
discovery field was added to separate org-level MCP access from
cross-org public catalog visibility.
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

    These tests act as a safety net: if someone adds a new billing
    mode without updating the ``clean()`` validation, the test suite
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
        assert "agent_public_discovery" not in form.fields
        assert "agent_billing_mode" not in form.fields
        assert "agent_price_cents" not in form.fields
        assert "agent_max_launches_per_hour" not in form.fields

    def test_superuser_form_includes_agent_fields(self):
        """Superusers should see all five agent access fields in the
        workflow form."""
        from validibot.workflows.forms import WorkflowForm

        form = WorkflowForm(user=self._make_user(is_superuser=True))
        assert "agent_access_enabled" in form.fields
        assert "agent_public_discovery" in form.fields
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

    def test_form_rejects_public_discovery_without_agent_access(self):
        """Public discovery without agent access enabled should fail.

        The form-level check mirrors the model's belt-and-suspenders
        validation: if someone submits the form with public_discovery=on
        but agent_access_enabled=off, the error should land on the
        public_discovery field."""
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
                "name": "Bad config workflow",
                "slug": "bad-config",
                "project": str(default_project.pk),
                "allowed_file_types": [SubmissionFileType.JSON],
                "data_retention": SubmissionRetention.DO_NOT_STORE,
                "output_retention": "STORE_30_DAYS",
                "version": "1.0",
                "is_active": "on",
                # agent_access_enabled is NOT checked
                "agent_public_discovery": "on",
                "agent_price_cents": "10",
            },
            user=user,
        )
        assert not form.is_valid()
        assert "agent_public_discovery" in form.errors

    def test_form_cascades_public_discovery_to_x402_billing(self):
        """Enabling public discovery should auto-set billing to x402.

        The user doesn't need to manually select the billing mode —
        public discovery implies agent-pays-x402 because external agents
        must pay per call."""
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
                "name": "Public discovery workflow",
                "slug": "public-disc",
                "project": str(default_project.pk),
                "allowed_file_types": [SubmissionFileType.JSON],
                "data_retention": SubmissionRetention.DO_NOT_STORE,
                "output_retention": "STORE_30_DAYS",
                "version": "1.0",
                "is_active": "on",
                "agent_access_enabled": "on",
                "agent_public_discovery": "on",
                "agent_billing_mode": AgentBillingMode.AUTHOR_PAYS,
                "agent_price_cents": "10",
            },
            user=user,
        )
        assert form.is_valid(), form.errors
        assert (
            form.cleaned_data["agent_billing_mode"] == AgentBillingMode.AGENT_PAYS_X402
        )


# ── Public discovery defaults ──────────────────────────────────────
# The agent_public_discovery flag defaults to False and must be
# explicitly enabled by a superuser.


class TestAgentPublicDiscoveryDefault:
    """Verify that agent_public_discovery defaults to False."""

    def test_defaults_to_false(self):
        """New workflows should not appear on the public agent catalog
        by default.  The superuser must explicitly opt in."""
        wf = WorkflowFactory.build()
        assert wf.agent_public_discovery is False


# ── clean() validation: public discovery constraints ───────────────
# These tests verify the cascade and constraint hierarchy for the
# agent_public_discovery field.


class TestAgentPublicDiscoveryConstraints:
    """Verify the constraint hierarchy for agent_public_discovery.

    The field has two cascading behaviors:
    - Enabling public discovery forces agent_billing_mode to X402
    - Disabling agent access forces public discovery off

    And one hard validation:
    - Public discovery requires agent_access_enabled=True
    """

    def test_public_discovery_requires_agent_access(self):
        """Attempting to enable public discovery without agent access
        should raise a ValidationError on agent_public_discovery.

        In practice this can only happen via the API/admin since the
        form also validates this, but the model enforces it as a
        belt-and-suspenders measure."""
        wf = WorkflowFactory.build(
            agent_access_enabled=False,
            agent_public_discovery=True,
            agent_price_cents=10,
        )
        wf.clean()
        assert wf.agent_public_discovery is False

    def test_public_discovery_forces_x402_billing(self):
        """Enabling public discovery should auto-set billing mode to
        AGENT_PAYS_X402, regardless of what was previously configured.

        This is a cascade, not a validation error — the model silently
        adjusts the billing mode so the constraint hierarchy is
        consistent."""
        wf = WorkflowFactory.build(
            agent_access_enabled=True,
            agent_public_discovery=True,
            agent_billing_mode=AgentBillingMode.AUTHOR_PAYS,
            agent_price_cents=10,
        )
        wf.clean()
        assert wf.agent_billing_mode == AgentBillingMode.AGENT_PAYS_X402

    def test_disabling_agent_access_clears_public_discovery(self):
        """When agent_access_enabled is set to False, clean() should
        cascade and clear agent_public_discovery too.

        This prevents an impossible state where the workflow is publicly
        discoverable but agents can't actually access it."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            agent_access_enabled=False,
            agent_public_discovery=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            data_retention=SubmissionRetention.DO_NOT_STORE,
        )
        wf.clean()
        assert wf.agent_public_discovery is False

    def test_full_valid_public_discovery_config(self):
        """The complete valid configuration for a publicly-discoverable
        workflow: agent access on, public discovery on, x402 billing,
        a price, and DO_NOT_STORE retention."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            agent_access_enabled=True,
            agent_public_discovery=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=50,
            data_retention=SubmissionRetention.DO_NOT_STORE,
        )
        wf.clean()  # should not raise
        assert wf.agent_public_discovery is True
        assert wf.agent_billing_mode == AgentBillingMode.AGENT_PAYS_X402

    def test_agent_access_enabled_without_public_discovery_is_valid(self):
        """agent_access_enabled=True + agent_public_discovery=False is
        the member-only MCP access configuration.  The workflow is
        visible to org members' agents but not on the public catalog."""
        wf = WorkflowFactory.build(
            agent_access_enabled=True,
            agent_public_discovery=False,
            agent_billing_mode=AgentBillingMode.AUTHOR_PAYS,
        )
        wf.clean()  # should not raise
        assert wf.agent_billing_mode == AgentBillingMode.AUTHOR_PAYS


# ── Tombstone clears public discovery ──────────────────────────────
# Tombstoning a workflow should clear both agent flags to prevent
# a deleted workflow from remaining on the public catalog.


class TestTombstoneClearsPublicDiscovery:
    """Verify that tombstoning clears agent_public_discovery."""

    def test_tombstone_clears_both_agent_flags(self):
        """Tombstoning a workflow should set both agent_access_enabled
        and agent_public_discovery to False.

        This prevents a race condition where a tombstoned workflow
        could briefly remain on the public catalog between the
        tombstone and the next cache refresh."""
        from validibot.submissions.constants import SubmissionRetention
        from validibot.users.tests.factories import UserFactory

        wf = WorkflowFactory(
            agent_access_enabled=True,
            agent_public_discovery=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=50,
            data_retention=SubmissionRetention.DO_NOT_STORE,
        )
        user = UserFactory()
        wf.tombstone(deleted_by=user, reason="test cleanup")

        wf.refresh_from_db()
        assert wf.agent_access_enabled is False
        assert wf.agent_public_discovery is False
