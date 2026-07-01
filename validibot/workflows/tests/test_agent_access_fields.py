"""
Tests for the workflow agent-access fields after the 2026-06-27 refactor.

The access model has THREE independent dials, all living on ``Workflow``:

- ``workflow_visibility`` (enum ``WorkflowVisibility``) — WHO, by Validibot
  identity, may run the workflow for free: PRIVATE / ORG / ALL_USERS.
- ``mcp_enabled`` (bool) — whether authenticated AI agents may run it via
  MCP, acting on behalf of a user who already has identity access. Billed
  to that user's plan quota.
- ``x402_enabled`` (bool) — whether the workflow is published for PAID,
  ANONYMOUS access via x402: anyone who pays the per-call price can run
  it, regardless of ``workflow_visibility``.

The headline of this refactor is that **x402 is INDEPENDENT of MCP**. The
old model coupled "public discovery" to "agent access" (discovery required
agent access; disabling agent access cascaded discovery off). That coupling
is GONE: ``x402_enabled=True`` with ``mcp_enabled=False`` is a valid,
first-class configuration (a workflow can be private to its org for
identity-scoped use while still being paid-public to anonymous agents).

The remaining invariants are about the x402 *billing rail*, not about MCP:

- Enabling ``x402_enabled`` forces ``agent_billing_mode=AGENT_PAYS_X402``
  (a cascade in ``clean()``).
- x402 billing requires a positive price.
- x402 billing requires ``input_retention=DO_NOT_STORE`` (privacy: x402 is
  anonymous per-call access, so storing submissions is incompatible).

Those invariants are enforced in ``clean()`` for normal saves AND lifted to
DB-level ``CheckConstraint`` rows so bulk paths (``QuerySet.update``,
fixtures, raw SQL) that bypass ``clean()`` still can't persist a
contradictory row.

History: the fields were renamed and decoupled in the 2026-06-27
access-control refactor (``agent_access_enabled`` → ``mcp_enabled``,
``agent_public_discovery`` → ``x402_enabled``). The DB constraints that used
to key on "public discovery requires agent access" were dropped; the x402
billing/price/retention/alive-row constraints were renamed onto
``x402_enabled``.
"""

import pytest
from django.core.exceptions import ValidationError

from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.constants import AgentBillingMode
from validibot.workflows.constants import WorkflowVisibility
from validibot.workflows.models import Workflow
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


# ── mcp_enabled default ─────────────────────────────────────────────
# By default, workflows are not exposed to authenticated agents via MCP.


class TestMcpEnabledDefault:
    """Verify that the MCP-access flag defaults to False.

    A new workflow must opt in to agent access; otherwise enabling a
    workflow for org members would silently expose it to their agents.
    """

    def test_defaults_to_false(self):
        """New workflows should not be visible to agents via MCP by
        default. The author must explicitly opt in."""
        wf = WorkflowFactory.build()
        assert wf.mcp_enabled is False


# ── clean() validation: MCP and billing are decoupled ───────────────
# ``mcp_enabled`` and ``agent_billing_mode`` are independent — any
# combination is valid.  The only constraint is that x402 billing
# requires a non-zero price (and DO_NOT_STORE retention, covered below).


class TestMcpBillingDecoupled:
    """Verify that ``mcp_enabled`` and ``agent_billing_mode`` are
    independent: enabling MCP access does not require any particular
    billing mode, and vice versa."""

    def test_enabled_with_author_pays_is_valid(self):
        """``mcp_enabled=True`` + AUTHOR_PAYS is the authenticated-only
        MCP access use case.  The author's plan quota covers agent
        usage — no x402 payment required."""
        wf = WorkflowFactory.build(
            mcp_enabled=True,
            agent_billing_mode=AgentBillingMode.AUTHOR_PAYS,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        wf.clean()  # should not raise

    def test_enabled_with_x402_and_price_is_valid(self):
        """``mcp_enabled=True`` + AGENT_PAYS_X402 + a price is a valid
        combination: the workflow is reachable by authenticated agents
        AND configured for the x402 billing rail."""
        wf = WorkflowFactory.build(
            mcp_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        wf.clean()  # should not raise

    def test_disabled_with_author_pays_is_valid(self):
        """The default state: MCP access disabled, AUTHOR_PAYS.  Nothing
        exposed, nothing to validate."""
        wf = WorkflowFactory.build(
            mcp_enabled=False,
            agent_billing_mode=AgentBillingMode.AUTHOR_PAYS,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        wf.clean()  # should not raise

    def test_disabled_with_x402_billing_configured_is_valid(self):
        """A workflow can have the x402 billing rail configured without
        MCP access enabled.  This allows staging the configuration before
        publishing."""
        wf = WorkflowFactory.build(
            mcp_enabled=False,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=25,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        wf.clean()  # should not raise


# ── clean() validation: x402 requires a price ──────────────────────
# This constraint applies whenever the x402 billing rail is selected,
# independent of the MCP flag, because a misconfigured price should be
# caught early.


class TestX402RequiresPrice:
    """Verify that selecting x402 billing without a valid price is
    rejected by clean().  This rule is independent of ``mcp_enabled``."""

    def test_x402_without_price_raises(self):
        """Setting the billing mode to x402 without a price is an error.
        Agents need to know how much to pay."""
        wf = WorkflowFactory.build(
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=None,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
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
        wf.project = ProjectFactory()  # project is required on Workflow now
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
            input_retention=SubmissionRetention.STORE_7_DAYS,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        with pytest.raises(ValidationError) as exc_info:
            wf.clean()
        assert "input_retention" in exc_info.value.message_dict

    def test_x402_with_store_permanently_raises(self):
        """Permanent storage is the most privacy-hostile pairing with
        x402 — explicitly reject it."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            input_retention=SubmissionRetention.STORE_PERMANENTLY,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        with pytest.raises(ValidationError) as exc_info:
            wf.clean()
        assert "input_retention" in exc_info.value.message_dict

    def test_x402_with_do_not_store_is_valid(self):
        """The only retention allowed alongside x402: immediate deletion
        after validation.  This preserves the privacy model."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        wf.clean()  # should not raise

    def test_author_pays_with_any_retention_is_valid(self):
        """The retention rule only applies to x402.  AUTHOR_PAYS has no
        anonymity guarantee to break, so any retention policy is fine."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            agent_billing_mode=AgentBillingMode.AUTHOR_PAYS,
            input_retention=SubmissionRetention.STORE_PERMANENTLY,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        wf.clean()  # should not raise


# ── Source resolution: removed ──────────────────────────────────────
#
# The previous implementation of source resolution accepted an
# ``X-Validibot-Source`` request header and trusted it.  That made
# ``ValidationRun.source`` a *caller-controlled* field — an analytics
# tag any client could spoof to "MCP" while invoking the plain API,
# polluting trust signals and pricing telemetry.
#
# The fix is to derive ``source`` from the authenticated route /
# auth channel itself (e.g. ``views.launch_api_validation_run``
# defaults to ``ValidationRunSource.API``, the MCP API view passes
# ``source=ValidationRunSource.MCP`` explicitly, x402 passes
# ``ValidationRunSource.X402_AGENT``).  No header read, no caller
# trust.  See tests in ``mcp_api`` and ``validibot_cloud.agents``
# for the per-route behaviour.


# ── Form: access fields gated by who-may-edit-access ────────────────
# The WorkflowForm conditionally adds the access controls (visibility +
# the two agent channels + billing companions). They appear only when
# the user is allowed to adjust access — an org admin in the workflow's org,
# OR the org has opted in via ``allow_authors_to_adjust_access``. Regular
# authors in a non-opted-in org should not see or be able to submit them.


class TestWorkflowFormAccessFields:
    """Verify that the access controls appear only for privileged users.

    The control set is the identity-scoped ``workflow_visibility`` dial
    plus the two independent agent channels (``mcp_enabled`` and
    ``x402_enabled``) and their billing/price/rate-limit companions.
    """

    def _make_user(self, *, is_superuser: bool = False):
        """Create a minimal user-like object for form instantiation.

        ``get_current_org`` returns None so the create-flow path resolves
        no org (and therefore no ceiling), which is enough for the
        field-presence assertions here.
        """
        from unittest.mock import Mock

        user = Mock()
        user.is_superuser = is_superuser
        user.is_staff = is_superuser
        user.is_authenticated = True
        user.get_current_org.return_value = None
        return user

    def test_non_privileged_user_form_excludes_access_fields(self):
        """A regular author (no org-admin role, org not opted in) should
        not see the access controls.  They are privileged: changing who
        can run a workflow — or publishing it for paid anonymous access —
        has security and billing consequences."""
        from validibot.workflows.forms import WorkflowForm

        form = WorkflowForm(user=self._make_user(is_superuser=False))
        assert "workflow_visibility" not in form.fields
        assert "mcp_enabled" not in form.fields
        assert "x402_enabled" not in form.fields
        assert "agent_billing_mode" not in form.fields
        assert "agent_price_cents" not in form.fields
        assert "agent_max_launches_per_hour" not in form.fields

    def test_platform_superuser_without_org_role_excludes_access_fields(self):
        """Django superuser/staff alone is not the access-policy admin gate."""
        from validibot.workflows.forms import WorkflowForm

        form = WorkflowForm(user=self._make_user(is_superuser=True))
        assert "workflow_visibility" not in form.fields
        assert "mcp_enabled" not in form.fields
        assert "x402_enabled" not in form.fields

    def test_org_admin_form_includes_access_fields(self):
        """Org admins should see the full access control set.

        Access policy is an organization decision, so the gate is the org
        admin role in the workflow's org rather than Django staff/superuser.
        """
        from validibot.workflows.forms import WorkflowForm

        org = OrganizationFactory()
        user = UserFactory()
        grant_role(user, org, RoleCode.ADMIN)
        user.set_current_org(org)

        form = WorkflowForm(user=user)
        assert "workflow_visibility" in form.fields
        assert "mcp_enabled" in form.fields
        assert "x402_enabled" in form.fields
        assert "agent_billing_mode" in form.fields
        assert "agent_price_cents" in form.fields
        assert "agent_max_launches_per_hour" in form.fields

    def test_privileged_create_omitting_access_fields_defaults_securely(self):
        """An admin create that OMITS the access fields must default securely.

        Org admins now see the access section, which made ``workflow_visibility``
        and ``agent_billing_mode`` present on the form. Both used to be
        ``required``, so any client that didn't submit them (a non-browser
        client, a minimal API/test POST) got a 400 instead of a workflow.
        This regression guard pins the fix: an omitted tier falls back to the
        secure model defaults (PRIVATE audience, AUTHOR_PAYS billing) rather
        than erroring — so omission can only NARROW access, never widen it or
        block creation.
        """
        from validibot.submissions.constants import SubmissionRetention
        from validibot.users.models import ensure_default_project
        from validibot.workflows.forms import WorkflowForm

        org = OrganizationFactory()
        user = UserFactory()
        grant_role(user, org, RoleCode.ADMIN)
        user.set_current_org(org)
        default_project = ensure_default_project(org)

        # Note: workflow_visibility and agent_billing_mode are deliberately
        # absent from the POST even though the form exposes them.
        form = WorkflowForm(
            data={
                "name": "Minimal create",
                "slug": "minimal-create",
                "project": str(default_project.pk),
                "allowed_file_types": [SubmissionFileType.JSON],
                "input_retention": SubmissionRetention.DO_NOT_STORE,
                "output_retention": "STORE_30_DAYS",
                "version": "1",
                "is_active": "on",
            },
            user=user,
        )
        # The form must accept the minimal POST (no 400), and ``clean()`` must
        # normalise the omitted access fields to the secure defaults rather
        # than leaving them blank. (``org``/``user`` are wired by the create
        # view at save time, so we assert on the validated cleaned data — the
        # exact output of the fallback logic — not a full DB save.)
        assert form.is_valid(), form.errors
        assert form.cleaned_data["workflow_visibility"] == WorkflowVisibility.PRIVATE
        assert form.cleaned_data["agent_billing_mode"] == AgentBillingMode.AUTHOR_PAYS

    def test_author_in_opted_in_org_sees_access_fields(self):
        """A non-superuser author whose org set
        ``allow_authors_to_adjust_access=True`` should also see the
        access controls — the org opted its authors in deliberately."""
        from validibot.users.models import ensure_default_project
        from validibot.workflows.forms import WorkflowForm

        org = OrganizationFactory(allow_authors_to_adjust_access=True)
        user = UserFactory(is_superuser=False, is_staff=False)
        grant_role(user, org, RoleCode.AUTHOR)
        user.set_current_org(org)
        ensure_default_project(org)

        form = WorkflowForm(user=user)
        assert "workflow_visibility" in form.fields
        assert "mcp_enabled" in form.fields
        assert "x402_enabled" in form.fields

    def test_form_rejects_x402_without_do_not_store(self):
        """A privileged-submitted form that pairs x402 billing with a
        non-DO_NOT_STORE retention should fail validation with the
        error attached to the ``input_retention`` field.

        This mirrors the model-level rule but surfaces the error on
        the form field so the UI can highlight the right control."""
        from validibot.submissions.constants import SubmissionRetention
        from validibot.users.models import ensure_default_project
        from validibot.workflows.forms import WorkflowForm

        # x402_allowed so the form lets the privileged user turn x402 on.
        org = OrganizationFactory(x402_allowed=True, mcp_allowed=True)
        user = UserFactory()
        grant_role(user, org, RoleCode.ADMIN)
        user.set_current_org(org)
        default_project = ensure_default_project(org)

        form = WorkflowForm(
            data={
                "name": "Anonymous agent workflow",
                "slug": "anon-agent",
                "project": str(default_project.pk),
                "allowed_file_types": [SubmissionFileType.JSON],
                "input_retention": SubmissionRetention.STORE_7_DAYS,
                "output_retention": "STORE_30_DAYS",
                "version": "1",
                "is_active": "on",
                "x402_enabled": "on",
                "agent_billing_mode": AgentBillingMode.AGENT_PAYS_X402,
                "agent_price_cents": "10",
            },
            user=user,
        )
        assert not form.is_valid()
        assert "input_retention" in form.errors

    def test_form_accepts_x402_with_do_not_store(self):
        """The valid combination: x402 enabled + DO_NOT_STORE retention."""
        from validibot.submissions.constants import SubmissionRetention
        from validibot.users.models import ensure_default_project
        from validibot.workflows.forms import WorkflowForm

        org = OrganizationFactory(x402_allowed=True, mcp_allowed=True)
        user = UserFactory()
        grant_role(user, org, RoleCode.ADMIN)
        user.set_current_org(org)
        default_project = ensure_default_project(org)

        form = WorkflowForm(
            data={
                "name": "Anonymous agent workflow",
                "slug": "anon-agent",
                "project": str(default_project.pk),
                "allowed_file_types": [SubmissionFileType.JSON],
                "input_retention": SubmissionRetention.DO_NOT_STORE,
                "output_retention": "STORE_30_DAYS",
                "version": "1",
                "is_active": "on",
                "workflow_visibility": WorkflowVisibility.ORG,
                "x402_enabled": "on",
                "agent_billing_mode": AgentBillingMode.AGENT_PAYS_X402,
                "agent_price_cents": "10",
            },
            user=user,
        )
        assert form.is_valid(), form.errors

    def test_form_disallows_x402_when_org_disallows_it(self):
        """A forged ``x402_enabled=on`` POST cannot publish a workflow
        when the org has not allowed the x402 channel.

        When ``org.x402_allowed`` is False the field is rendered with
        ``disabled=True``. Django ignores a submitted value for a disabled
        field and falls back to its initial (False), so the forged value
        never takes effect — and ``clean()`` re-checks the org guardrail
        as defence in depth. Either way, the saved workflow must NOT end
        up x402-published, which is the security property we care about."""
        from validibot.submissions.constants import SubmissionRetention
        from validibot.users.models import ensure_default_project
        from validibot.workflows.forms import WorkflowForm

        # x402 NOT allowed for this org, even though the user is privileged.
        org = OrganizationFactory(x402_allowed=False, mcp_allowed=True)
        user = UserFactory()
        grant_role(user, org, RoleCode.ADMIN)
        user.set_current_org(org)
        default_project = ensure_default_project(org)

        form = WorkflowForm(
            data={
                "name": "Forged x402 workflow",
                "slug": "forged-x402",
                "project": str(default_project.pk),
                "allowed_file_types": [SubmissionFileType.JSON],
                "input_retention": SubmissionRetention.DO_NOT_STORE,
                "output_retention": "STORE_30_DAYS",
                "version": "1",
                "is_active": "on",
                "workflow_visibility": WorkflowVisibility.ORG,
                "x402_enabled": "on",
                "agent_billing_mode": AgentBillingMode.AUTHOR_PAYS,
            },
            user=user,
        )
        # The disabled field coerces x402 off, so the form itself is valid;
        # the forged "on" simply has no effect.
        assert form.is_valid(), form.errors
        assert form.cleaned_data.get("x402_enabled") is False

    def test_form_cascades_x402_to_x402_billing(self):
        """Enabling x402 should auto-set billing to AGENT_PAYS_X402.

        The user doesn't need to manually select the billing mode —
        publishing for paid anonymous access implies the x402 billing
        rail.  This mirrors the model's ``clean()`` cascade.  (x402 and
        MCP are independent, so this does NOT touch ``mcp_enabled``.)"""
        from validibot.submissions.constants import SubmissionRetention
        from validibot.users.models import ensure_default_project
        from validibot.workflows.forms import WorkflowForm

        org = OrganizationFactory(x402_allowed=True, mcp_allowed=True)
        user = UserFactory()
        grant_role(user, org, RoleCode.ADMIN)
        user.set_current_org(org)
        default_project = ensure_default_project(org)

        form = WorkflowForm(
            data={
                "name": "Paid anonymous workflow",
                "slug": "paid-anon",
                "project": str(default_project.pk),
                "allowed_file_types": [SubmissionFileType.JSON],
                "input_retention": SubmissionRetention.DO_NOT_STORE,
                "output_retention": "STORE_30_DAYS",
                "version": "1",
                "is_active": "on",
                "workflow_visibility": WorkflowVisibility.ORG,
                "x402_enabled": "on",
                # Author left billing on the default — the cascade overrides.
                "agent_billing_mode": AgentBillingMode.AUTHOR_PAYS,
                "agent_price_cents": "10",
            },
            user=user,
        )
        assert form.is_valid(), form.errors
        assert (
            form.cleaned_data["agent_billing_mode"] == AgentBillingMode.AGENT_PAYS_X402
        )


# ── x402_enabled default ───────────────────────────────────────────
# The x402 (paid anonymous) flag defaults to False and must be
# explicitly enabled by a privileged user.


class TestX402EnabledDefault:
    """Verify that ``x402_enabled`` defaults to False.

    Publishing a workflow to the public, paying internet is a deliberate
    act — it must never be the default.
    """

    def test_defaults_to_false(self):
        """New workflows should not be published for paid anonymous
        access by default.  The author must explicitly opt in."""
        wf = WorkflowFactory.build()
        assert wf.x402_enabled is False


class TestWorkflowVisibilityDefault:
    """New workflows must be private at the model layer, not only in forms."""

    def test_model_field_defaults_to_private(self):
        """A raw model instance should default to PRIVATE visibility.

        This catches create paths that bypass the workflow form's initial
        value, such as API/import/admin code paths. The test deliberately
        avoids ``WorkflowFactory`` because that fixture stays ORG-visible for
        older org-member behavior tests.
        """
        org = OrganizationFactory()
        user = UserFactory()
        workflow = Workflow(
            org=org,
            user=user,
            project=ProjectFactory(org=org),
            name="Default private workflow",
        )

        assert workflow.workflow_visibility == WorkflowVisibility.PRIVATE


# ── clean(): x402 is INDEPENDENT of MCP (the decoupling) ────────────
# The headline of the 2026-06-27 refactor: there is no coupling between
# ``x402_enabled`` and ``mcp_enabled``.  The old "discovery requires
# agent access" rule and the "disabling agent access clears discovery"
# cascade are GONE.  These tests pin the new independence.


class TestX402IndependentOfMcp:
    """Verify that ``x402_enabled`` and ``mcp_enabled`` are fully
    independent dials after the decoupling refactor.

    A workflow can be paid-public to anonymous agents (x402) while being
    closed to authenticated MCP agents (and vice versa). The only thing
    enabling x402 does is select the x402 billing rail; it never touches
    ``mcp_enabled``.
    """

    def test_x402_without_mcp_is_valid(self):
        """``x402_enabled=True`` with ``mcp_enabled=False`` is now VALID.

        Under the old model this was an impossible state (public
        discovery required agent access). The decoupling makes it a
        first-class config: a workflow private to authenticated agents
        but open to anyone who pays via x402."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            x402_enabled=True,
            mcp_enabled=False,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        wf.clean()  # should NOT raise — the coupling is gone
        # clean() must not have flipped mcp_enabled on as a side effect.
        assert wf.mcp_enabled is False
        assert wf.x402_enabled is True

    def test_enabling_x402_forces_x402_billing(self):
        """Enabling x402 should auto-set billing mode to AGENT_PAYS_X402,
        regardless of what was previously configured.

        This is a cascade, not a validation error — the model silently
        selects the x402 billing rail so the price/retention guards apply.
        It mirrors the form cascade. Note this does NOT couple to MCP."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            x402_enabled=True,
            mcp_enabled=False,
            agent_billing_mode=AgentBillingMode.AUTHOR_PAYS,
            agent_price_cents=10,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        wf.clean()
        assert wf.agent_billing_mode == AgentBillingMode.AGENT_PAYS_X402

    def test_disabling_mcp_does_not_clear_x402(self):
        """Turning ``mcp_enabled`` off must NOT cascade ``x402_enabled``
        off.

        The old model cleared "public discovery" when "agent access" was
        disabled. That cascade is gone — the two are independent, so an
        x402-published workflow stays published even with MCP off."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            mcp_enabled=False,
            x402_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        wf.clean()
        # x402 must survive — no coupling to mcp_enabled.
        assert wf.x402_enabled is True

    def test_full_valid_x402_config(self):
        """The complete valid configuration for a paid-public workflow:
        x402 on, x402 billing, a price, and DO_NOT_STORE retention.
        MCP can be anything — here it's off to show independence."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory.build(
            x402_enabled=True,
            mcp_enabled=False,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=50,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        wf.clean()  # should not raise
        assert wf.x402_enabled is True
        assert wf.agent_billing_mode == AgentBillingMode.AGENT_PAYS_X402

    def test_mcp_enabled_without_x402_is_valid(self):
        """``mcp_enabled=True`` + ``x402_enabled=False`` is the
        authenticated-only MCP access configuration.  The workflow is
        runnable by org members' agents but not published to the paying
        public."""
        wf = WorkflowFactory.build(
            mcp_enabled=True,
            x402_enabled=False,
            agent_billing_mode=AgentBillingMode.AUTHOR_PAYS,
        )
        wf.project = ProjectFactory()  # project is required on Workflow now
        wf.clean()  # should not raise
        assert wf.agent_billing_mode == AgentBillingMode.AUTHOR_PAYS


# ── Tombstone clears both agent channels ────────────────────────────
# Tombstoning a workflow should clear both ``mcp_enabled`` and
# ``x402_enabled`` to prevent a deleted workflow from remaining reachable
# by any agent channel.


class TestTombstoneClearsAgentChannels:
    """Verify that tombstoning clears ``mcp_enabled`` and ``x402_enabled``."""

    def test_tombstone_clears_both_agent_flags(self):
        """Tombstoning a workflow should set both ``mcp_enabled`` and
        ``x402_enabled`` to False.

        This prevents a published x402 workflow (or an MCP-reachable one)
        from lingering on an agent surface after the break-glass removal,
        and it keeps the row out of the contradictory "x402_enabled on an
        archived/tombstoned row" state the alive-row constraint forbids."""
        from validibot.submissions.constants import SubmissionRetention
        from validibot.users.tests.factories import UserFactory

        wf = WorkflowFactory(
            mcp_enabled=True,
            x402_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=50,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )
        user = UserFactory()
        wf.tombstone(deleted_by=user, reason="test cleanup")

        wf.refresh_from_db()
        assert wf.mcp_enabled is False
        assert wf.x402_enabled is False


# ──────────────────────────────────────────────────────────────────────
# DB-level CheckConstraint enforcement
# ──────────────────────────────────────────────────────────────────────
#
# ``Workflow.clean()`` enforces the x402 publishing invariants (x402
# billing requires positive price, x402 requires DO_NOT_STORE retention,
# x402 publish implies x402 billing, x402 publish implies an alive row),
# but ``clean()`` does NOT fire on:
#   • ``QuerySet.update()`` (admin bulk edits)
#   • Fixtures / ``loaddata``
#   • Raw SQL writes
#
# Those invariants are therefore lifted to DB-level ``CheckConstraint``
# rows so the constraint survives every write path. After the
# decoupling, the constraints are keyed on ``x402_enabled`` /
# ``agent_billing_mode``; the old "public discovery requires agent
# access" constraint was DROPPED because that coupling no longer exists.
# These tests exercise the bypass path (``QuerySet.update``) explicitly:
# if any constraint regresses, ``IntegrityError`` should be raised — and
# one of these tests will fail loudly if it isn't.


class TestWorkflowCheckConstraints:
    """Trust-critical x402 publishing invariants enforced at the DB layer."""

    def test_x402_without_mcp_persists_via_update(self):
        """The DB does NOT couple ``x402_enabled`` to ``mcp_enabled``.

        After the decoupling there is no constraint requiring MCP for an
        x402 row. A bulk ``update`` that turns x402 on while MCP is off
        must succeed — this pins the *absence* of the old coupling
        constraint at the DB layer (the inverse of the dropped
        ``ck_workflow_public_discovery_requires_agent_access``)."""
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory(
            mcp_enabled=False,
            x402_enabled=False,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )
        # x402 on, MCP off, with the billing rail + price the other
        # constraints require — must persist without IntegrityError.
        Workflow.objects.filter(pk=wf.pk).update(
            x402_enabled=True,
            mcp_enabled=False,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
        )
        wf.refresh_from_db()
        assert wf.x402_enabled is True
        assert wf.mcp_enabled is False

    def test_x402_billing_requires_positive_price_via_update(self):
        """x402 billing mode cannot coexist with a zero / null price."""
        from django.db import IntegrityError
        from django.db import transaction

        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory(
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=100,
            mcp_enabled=True,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )

        with transaction.atomic(), pytest.raises(IntegrityError):
            Workflow.objects.filter(pk=wf.pk).update(agent_price_cents=0)

    def test_x402_billing_requires_do_not_store_retention_via_update(self):
        """x402 billing mode cannot coexist with retention != DO_NOT_STORE.

        x402 is anonymous per-call payment — storing the input
        undermines the privacy contract the workflow author agreed to
        when they enabled x402. The DB constraint guarantees no row
        ever sits in the contradictory state, regardless of how it
        was written.
        """
        from django.db import IntegrityError
        from django.db import transaction

        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory(
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=100,
            mcp_enabled=True,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )

        with transaction.atomic(), pytest.raises(IntegrityError):
            Workflow.objects.filter(pk=wf.pk).update(
                input_retention=SubmissionRetention.STORE_30_DAYS,
            )

    def test_non_x402_workflows_can_have_any_retention(self):
        """The retention constraint only fires when billing_mode = X402.

        AUTHOR_PAYS workflows authenticate the caller and storing
        their submissions is fine — the constraint must only apply
        to the x402 (anonymous) path.
        """
        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory(
            agent_billing_mode=AgentBillingMode.AUTHOR_PAYS,
        )
        # Should succeed without IntegrityError — no constraint violation.
        Workflow.objects.filter(pk=wf.pk).update(
            input_retention=SubmissionRetention.STORE_30_DAYS,
        )
        wf.refresh_from_db()
        assert wf.input_retention == SubmissionRetention.STORE_30_DAYS

    def test_x402_billing_rejects_null_price_via_update(self):
        """x402 + NULL price is a contradiction the DB must refuse.

        SQL CHECK constraints treat ``NULL > 0`` as UNKNOWN (not
        FALSE), so a naive ``agent_price_cents > 0`` clause silently
        passes for x402 rows with NULL prices.  The constraint adds
        an explicit ``IS NOT NULL`` clause to close that hole.
        """
        from django.db import IntegrityError
        from django.db import transaction

        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory(
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=100,
            mcp_enabled=True,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )

        with transaction.atomic(), pytest.raises(IntegrityError):
            Workflow.objects.filter(pk=wf.pk).update(agent_price_cents=None)

    def test_x402_enabled_requires_x402_billing_via_update(self):
        """An ``x402_enabled=True`` row must use x402 billing.

        A row with ``x402_enabled=True`` but
        ``agent_billing_mode=AUTHOR_PAYS`` is a contradiction —
        the public x402 surface is the anonymous-payment rail, so
        AUTHOR_PAYS doesn't apply there.  ``clean()`` rejects this
        for normal saves; the ``ck_workflow_x402_enabled_requires_x402_billing``
        DB constraint catches the bulk-update bypass.
        """
        from django.db import IntegrityError
        from django.db import transaction

        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory(
            x402_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )

        with transaction.atomic(), pytest.raises(IntegrityError):
            Workflow.objects.filter(pk=wf.pk).update(
                agent_billing_mode=AgentBillingMode.AUTHOR_PAYS,
            )

    def test_x402_enabled_blocked_on_archived_row_via_update(self):
        """An archived row must not retain ``x402_enabled=True``.

        The ``ck_workflow_x402_enabled_requires_alive_row`` constraint
        forbids the contradictory "published but archived" state, so a
        bulk ``update`` that archives an x402-published row must raise."""
        from django.db import IntegrityError
        from django.db import transaction

        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory(
            x402_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )

        with transaction.atomic(), pytest.raises(IntegrityError):
            Workflow.objects.filter(pk=wf.pk).update(is_archived=True)

    def test_x402_enabled_blocked_on_tombstoned_row_via_update(self):
        """A tombstoned row must not retain ``x402_enabled=True``.

        ``tombstone()`` clears the flag, but the alive-row DB constraint
        defends the invariant against bypass paths that don't go
        through the tombstone helper.
        """
        from django.db import IntegrityError
        from django.db import transaction

        from validibot.submissions.constants import SubmissionRetention

        wf = WorkflowFactory(
            x402_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )

        with transaction.atomic(), pytest.raises(IntegrityError):
            Workflow.objects.filter(pk=wf.pk).update(is_tombstoned=True)
