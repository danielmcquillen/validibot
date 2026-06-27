"""Guest access, invites, and sharing views.

Views for accepting workflow invites, listing guest workflows, managing
sharing settings (visibility, guest access grants, invitations), and
invite lifecycle (create, cancel, resend, revoke).
"""

import logging
from http import HTTPStatus

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import ListView
from django.views.generic import TemplateView

from validibot.core.mixins import GuestInvitesEnabledMixin
from validibot.core.utils import reverse_with_org
from validibot.workflows.mixins import WorkflowObjectMixin

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Family-scoped sharing helpers
# ──────────────────────────────────────────────────────────────────────
#
# The permission layer treats an active grant on ANY version of a
# workflow's ``(org_id, slug)`` family as authorisation for every
# version (see ``WorkflowQuerySet.for_user`` and the per-row checks
# on ``Workflow``).  Without family-scoped queries here the
# management surface diverges from the permission rule and produces
# three bugs:
#
#   (a) The sharing page on v2 hides grants rooted on v1 even
#       though the holder still has access to v2.
#   (b) The "user already has access" duplicate check misses legacy
#       v1 grants when a manager invites the same email on v2.
#   (c) Revoking a v2 row leaves the user's v1 grant intact, and
#       the family-grant rule keeps v2 visible to them — the UI
#       shows "revoked" while access is still live.
#
# These helpers return querysets scoped to the entire family, with
# at most one row per user (the most recent grant), so the listing,
# duplicate check, and revoke flow all share the family rule.


def _family_active_grants_for_listing(workflow):
    """Return active grants in the family with one row per user.

    Uses PostgreSQL ``DISTINCT ON`` to pick the most recent grant per
    user; the wrapping queryset re-sorts the result by ``-created``
    for stable display order.
    """
    from validibot.workflows.models import WorkflowAccessGrant

    distinct_pks = (
        WorkflowAccessGrant.objects.filter(
            workflow__org_id=workflow.org_id,
            workflow__slug=workflow.slug,
            is_active=True,
        )
        # DISTINCT ON requires the deduplication key (user_id) to be
        # the leading order_by; the secondary ``-created`` selects
        # the most recent grant per user.
        .order_by("user_id", "-created")
        .distinct("user_id")
        .values_list("pk", flat=True)
    )
    return (
        WorkflowAccessGrant.objects.filter(pk__in=list(distinct_pks))
        .select_related("user", "granted_by", "workflow")
        .order_by("-created")
    )


def _family_pending_invites_for_listing(workflow):
    """Return pending invites in the family with one row per email.

    Same DISTINCT ON pattern as :func:`_family_active_grants_for_listing`.
    """
    from validibot.workflows.models import WorkflowInvite

    distinct_pks = (
        WorkflowInvite.objects.filter(
            workflow__org_id=workflow.org_id,
            workflow__slug=workflow.slug,
            status=WorkflowInvite.Status.PENDING,
        )
        .order_by("invitee_email", "-created")
        .distinct("invitee_email")
        .values_list("pk", flat=True)
    )
    return (
        WorkflowInvite.objects.filter(pk__in=list(distinct_pks))
        .select_related("inviter", "invitee_user", "workflow")
        .order_by("-created")
    )


def _render_family_guest_section(request, workflow, view):
    """Render the guest access section with family-scoped data.

    Extracted so all 4 sharing views render the same section the
    same way; the previous code duplicated this block 4 times with
    subtle differences (some had HX-Trigger headers, some didn't).
    """
    context = {
        "workflow": workflow,
        "access_grants": _family_active_grants_for_listing(workflow),
        "pending_invites": _family_pending_invites_for_listing(workflow),
        "can_manage_sharing": view.user_can_manage_sharing(),
    }
    return render(
        request,
        "workflows/partials/workflow_guest_access_section.html",
        context,
    )


# Workflow Invite Views
# ------------------------------------------------------------------------------


class WorkflowInviteAcceptView(GuestInvitesEnabledMixin, View):
    """
    Handle workflow invite acceptance.

    This view handles the invite accept flow:
    1. For logged-in users: Accepts the invite immediately and redirects to workflow
    2. For anonymous users: Stores the invite token in session and redirects to signup

    The invite token is passed as a URL parameter.

    Gated by ``GuestInvitesEnabledMixin``: when
    ``SiteSettings.allow_guest_invites`` is False, redemption of even
    a previously-issued invite is blocked. The invite row remains
    PENDING in the database; flipping the flag back on restores
    acceptance until expiry.
    """

    WORKFLOW_INVITE_SESSION_KEY = "workflow_invite_token"
    template_name = "workflows/workflow_invite_confirm.html"

    def _resolve_pending_invite(self, request, token):
        """Resolve the invite and ensure it is still redeemable.

        Returns ``(invite, None)`` for a pending invite, or
        ``(None, response)`` when the caller should bail out (invalid or
        expired invite → redirect home). Read-only, so it is safe to run on
        both GET and POST.
        """
        from validibot.workflows.models import WorkflowInvite

        invite = get_object_or_404(
            WorkflowInvite.objects.select_related("workflow", "inviter"),
            token=token,
        )
        if not invite.is_pending:
            messages.error(
                request,
                _("This invite is no longer valid (status: %(status)s).")
                % {"status": invite.get_status_display()},
            )
            return None, HttpResponseRedirect(reverse("home:home"))
        return invite, None

    def _wrong_email_redirect(self, request, invite):
        """Guard against cross-account redemption of an email-only invite.

        An email-only invite (``invitee_user`` is None) must only be redeemed
        by an account that provably owns the invited email — otherwise a leaked
        or forwarded link lets whoever opens it claim access addressed to a
        different person. Returns a redirect response on mismatch, else None.
        Read-only; mirrors ``MemberInviteAcceptView``.
        """
        from validibot.members.views import user_owns_invited_email

        if invite.invitee_user_id is None and not user_owns_invited_email(
            request.user,
            invite.invitee_email,
        ):
            messages.error(
                request,
                _(
                    "This invitation is addressed to a different email "
                    "address, so it was not applied to your account."
                ),
            )
            return HttpResponseRedirect(reverse("home:home"))
        return None

    def _redirect_anonymous_to_signup(self, request, token, invite):
        """Stash the token and send an anonymous visitor to signup.

        The post-signup adapter consumes the stashed token and applies the
        invite once the account exists.
        """
        request.session[self.WORKFLOW_INVITE_SESSION_KEY] = str(token)
        messages.info(
            request,
            _(
                "Please sign up or log in to accept your invitation "
                "to workflow '%(name)s'."
            )
            % {"name": invite.workflow.name},
        )
        return HttpResponseRedirect(reverse("account_signup"))

    def get(self, request, token):
        """Show a confirmation page — never accept on GET.

        Accepting on GET is a CSRF-class hole: an ``<img src="…invite…">`` on
        any page a logged-in invitee visits would silently accept the invite.
        GET is therefore side-effect-free — it resolves the invite and renders
        a confirm page whose button POSTs back here. (ADR 04-23 #8.)
        """
        invite, bail = self._resolve_pending_invite(request, token)
        if bail is not None:
            return bail

        if not request.user.is_authenticated:
            return self._redirect_anonymous_to_signup(request, token, invite)

        wrong_email = self._wrong_email_redirect(request, invite)
        if wrong_email is not None:
            return wrong_email

        return render(
            request,
            self.template_name,
            {"invite": invite, "workflow": invite.workflow},
        )

    def post(self, request, token):
        """Accept the invite — the state change lives only here (CSRF-protected)."""
        invite, bail = self._resolve_pending_invite(request, token)
        if bail is not None:
            return bail

        if not request.user.is_authenticated:
            return self._redirect_anonymous_to_signup(request, token, invite)

        wrong_email = self._wrong_email_redirect(request, invite)
        if wrong_email is not None:
            return wrong_email

        try:
            grant = invite.accept(user=request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
            return HttpResponseRedirect(reverse("home:home"))

        from validibot.workflows.emails import send_workflow_invite_accepted_email

        send_workflow_invite_accepted_email(grant)
        messages.success(
            request,
            _(
                "You now have access to the workflow '%(name)s'. "
                "You can run validations on this workflow."
            )
            % {"name": invite.workflow.name},
        )
        return HttpResponseRedirect(
            reverse(
                "workflows:workflow_launch",
                kwargs={"pk": invite.workflow.pk},
            ),
        )


class GuestInviteAcceptView(GuestInvitesEnabledMixin, View):
    """Handle org-level guest invite acceptance via tokenized URL.

    Mirrors :class:`WorkflowInviteAcceptView` for the org-level
    ``GuestInvite`` flow:

    1. **Logged-in users** — GET shows a confirmation page; acceptance is a
       POST (CSRF-protected). Accepting returns a single ``OrgGuestAccess``
       row for ALL scope or per-workflow grants for SELECTED scope, then
       redirects to the guest-workflows listing.
    2. **Anonymous users** — stash the token in the session and
       redirect to signup. After signup, the
       :class:`~validibot.users.adapters.AccountAdapter` consumes the
       token, calls ``invite.accept()`` on the new user, and
       classifies them as GUEST (sticky semantics).

    Without this view there was no anonymous-friendly redemption
    path: the email pointed at ``/notifications/`` (which an
    unauthenticated user can't see), and the only way to accept was
    to already have an account and a notification — useless for a
    brand-new external collaborator.

    The ``GuestInvitesEnabledMixin`` is the operator's site-wide
    kill switch: even with a valid token, redemption is denied while
    ``allow_guest_invites=False``. The invite row stays PENDING and
    can be redeemed once the flag is flipped back on (assuming it
    hasn't expired).
    """

    GUEST_INVITE_SESSION_KEY = "guest_invite_token"
    template_name = "workflows/guest_invite_confirm.html"

    def _resolve_pending_invite(self, request, token):
        """Resolve the guest invite and ensure it is still redeemable.

        Returns ``(invite, None)`` for a pending invite, or
        ``(None, response)`` when the caller should bail out (invalid or
        expired invite → redirect home). Read-only; safe on GET and POST.
        """
        from validibot.workflows.models import GuestInvite

        invite = get_object_or_404(
            GuestInvite.objects.select_related("org", "inviter"),
            token=token,
        )
        if not invite.is_pending:
            messages.error(
                request,
                _("This guest invite is no longer valid (status: %(status)s).")
                % {"status": invite.get_status_display()},
            )
            return None, HttpResponseRedirect(reverse("home:home"))
        return invite, None

    def _wrong_email_redirect(self, request, invite):
        """Guard against cross-account redemption of an email-only invite.

        SECURITY: see WorkflowInviteAcceptView — an email-only guest invite
        (``invitee_user`` is None) may only be redeemed by an account that
        provably owns the invited email, so a leaked link cannot grant another
        person's org guest access to whoever opens it. Read-only.
        """
        from validibot.members.views import user_owns_invited_email

        if invite.invitee_user_id is None and not user_owns_invited_email(
            request.user,
            invite.invitee_email,
        ):
            messages.error(
                request,
                _(
                    "This guest invitation is addressed to a different "
                    "email address, so it was not applied to your account."
                ),
            )
            return HttpResponseRedirect(reverse("home:home"))
        return None

    def _redirect_anonymous_to_signup(self, request, token, invite):
        """Stash the token and route an anonymous visitor through signup.

        The AccountAdapter consumes the session key after signup completes,
        calls ``invite.accept()`` on the new user, and classifies them as GUEST.
        """
        request.session[self.GUEST_INVITE_SESSION_KEY] = str(token)
        messages.info(
            request,
            _("Please sign up or log in to accept your guest invitation to %(org)s.")
            % {"org": invite.org.name},
        )
        return HttpResponseRedirect(reverse("account_signup"))

    def get(self, request, token):
        """Show a confirmation page — never accept on GET.

        Same CSRF-class reasoning as ``WorkflowInviteAcceptView``: accepting on
        GET would let an ``<img src="…invite…">`` silently grant org guest
        access to a logged-in invitee. GET is side-effect-free; the confirm
        page's button POSTs back here. (ADR 04-23 #8, sibling view.)
        """
        invite, bail = self._resolve_pending_invite(request, token)
        if bail is not None:
            return bail

        if not request.user.is_authenticated:
            return self._redirect_anonymous_to_signup(request, token, invite)

        wrong_email = self._wrong_email_redirect(request, invite)
        if wrong_email is not None:
            return wrong_email

        return render(
            request,
            self.template_name,
            {"invite": invite, "org": invite.org},
        )

    def post(self, request, token):
        """Accept the guest invite.

        The state change lives only here (CSRF-protected).
        """
        invite, bail = self._resolve_pending_invite(request, token)
        if bail is not None:
            return bail

        if not request.user.is_authenticated:
            return self._redirect_anonymous_to_signup(request, token, invite)

        wrong_email = self._wrong_email_redirect(request, invite)
        if wrong_email is not None:
            return wrong_email

        try:
            invite.accept(user=request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
            return HttpResponseRedirect(reverse("home:home"))

        messages.success(
            request,
            _("You now have guest access to %(org)s.") % {"org": invite.org.name},
        )
        # Guest-workflow listing is the dedicated guest-friendly surface;
        # sending the user there avoids landing them on views that require
        # active memberships.
        return HttpResponseRedirect(reverse("workflows:guest_workflow_list"))


# Guest Workflow Views
# ------------------------------------------------------------------------------


class GuestWorkflowListView(LoginRequiredMixin, ListView):
    """
    List workflows that a guest user has access to via WorkflowAccessGrants.

    This view is for workflow guests (users with grants but no org memberships).
    It shows workflows from all organizations the user has been granted access to,
    with the org name displayed on each workflow card.
    """

    template_name = "workflows/guest_workflow_list.html"
    context_object_name = "workflows"
    paginate_by = 20

    def get_queryset(self):
        from validibot.workflows.models import Workflow

        # Get workflows the user has grants for
        return (
            Workflow.objects.for_user(self.request.user)
            .filter(is_archived=False, is_active=True, is_tombstoned=False)
            .select_related("org", "project")
            .order_by("org__name", "name")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_query"] = self.request.GET.get("q", "")
        return context


class WorkflowSharingView(WorkflowObjectMixin, TemplateView):
    """
    View for managing workflow sharing settings (visibility and guest access).

    This is the "Sharing" tab in workflow settings. It allows:
    - Setting workflow visibility (private/public)
    - Viewing/managing guest access grants
    - Inviting guests to this workflow
    """

    template_name = "workflows/workflow_sharing.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_sharing():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()

        # Family-scoped queries — see module docstring above for why.
        # An active grant on any version of this workflow's family
        # authorises the entire family (see permissions.py), so the
        # sharing page must surface those grants on every version's
        # page.  The helpers also dedupe to one row per user/email.
        context.update(
            {
                "access_grants": _family_active_grants_for_listing(workflow),
                "pending_invites": _family_pending_invites_for_listing(workflow),
            },
        )
        # The visibility-section signals (clamped choices, org ceilings,
        # access-edit gate) come from the shared builder so the initial
        # page render and the HTMX re-render after a POST stay identical.
        context.update(
            _build_visibility_section_context(self, workflow, self.request),
        )
        return context

    def get_breadcrumbs(self):
        workflow = self.get_workflow()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Workflows"),
                "url": reverse_with_org(
                    "workflows:workflow_list",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append(
            self.workflow_breadcrumb_item(
                workflow,
                url=reverse_with_org(
                    "workflows:workflow_detail",
                    request=self.request,
                    kwargs={"pk": workflow.pk},
                ),
            ),
        )
        breadcrumbs.append({"name": _("Sharing")})
        return breadcrumbs


def _user_can_edit_workflow_access(user, workflow) -> bool:
    """Return True when ``user`` may edit ``workflow``'s access controls.

    Mirrors ``WorkflowForm._user_can_edit_access``: the per-workflow
    access controls (visibility tier + the MCP / x402 agent channels) are
    privileged. They may be changed only by a Django superuser/staff
    member, OR when the workflow's organization has opted in via
    ``Organization.allow_authors_to_adjust_access``.

    This is separate from ``user_can_manage_sharing`` (which gates the
    guest-invite surface): a workflow author can always invite guests to
    their own workflow, but may only retune the access tier / agent
    channels when their org allows it.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    org = getattr(workflow, "org", None)
    return bool(org and getattr(org, "allow_authors_to_adjust_access", False))


def _build_visibility_section_context(view, workflow, request):
    """Assemble the context the visibility section partial renders with.

    Centralised so the GET-rendered page and the HTMX re-render after a
    POST stay in lock-step. Surfaces the clamped visibility choices, the
    org's MCP / x402 ceilings, and whether the current user may edit the
    access controls — all read-only signals the template uses to decide
    what to show, disable, or hide.
    """
    from validibot.workflows.constants import WORKFLOW_VISIBILITY_ORDER
    from validibot.workflows.constants import WorkflowVisibility
    from validibot.workflows.constants import visibility_within_cap

    org = workflow.org
    cap = getattr(org, "workflow_visibility_cap", None) or WorkflowVisibility.ALL_USERS
    labels = dict(WorkflowVisibility.choices)
    visibility_choices = [
        {"value": value, "label": labels[value]}
        for value in WORKFLOW_VISIBILITY_ORDER
        if visibility_within_cap(value, cap)
    ]
    return {
        "workflow": workflow,
        "can_manage_sharing": view.user_can_manage_sharing(),
        "can_edit_access": _user_can_edit_workflow_access(request.user, workflow),
        "visibility_choices": visibility_choices,
        "org_mcp_allowed": bool(getattr(org, "mcp_allowed", False)),
        "org_x402_allowed": bool(getattr(org, "x402_allowed", False)),
    }


class WorkflowVisibilityUpdateView(WorkflowObjectMixin, View):
    """Update a workflow's identity-scoped visibility and agent channels.

    Replaces the old binary public/private toggle. The POST sets the
    three-way ``workflow_visibility`` tier (clamped to the org ceiling)
    and, when permitted, the two INDEPENDENT agent channels: ``mcp_enabled``
    (authenticated agents on behalf of a user) and ``x402_enabled`` (paid
    anonymous public access). Each agent channel is honoured only when the
    org allows it AND the user passes the access-edit gate, so a forged
    POST cannot widen a workflow beyond what its org and role permit.
    """

    def post(self, request, *args, **kwargs):
        from validibot.workflows.constants import WorkflowVisibility
        from validibot.workflows.constants import visibility_within_cap

        if not self.user_can_manage_sharing():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        workflow = self.get_workflow()
        org = workflow.org
        changed_fields: list[str] = []

        # ── Visibility (the WHO dial) ─────────────────────────────────
        # Validate against the enum, then CLAMP to the org ceiling so a
        # submitted tier can never exceed ``workflow_visibility_cap`` even
        # if the client forged a wider value. An unknown value is ignored
        # (leaves the current tier untouched) rather than erroring.
        raw_visibility = (request.POST.get("workflow_visibility") or "").strip()
        if raw_visibility in WorkflowVisibility.values:
            cap = (
                getattr(org, "workflow_visibility_cap", None)
                or WorkflowVisibility.ALL_USERS
            )
            if not visibility_within_cap(raw_visibility, cap):
                # Requested wider than allowed — pin to the ceiling.
                raw_visibility = cap
            if workflow.workflow_visibility != raw_visibility:
                workflow.workflow_visibility = raw_visibility
                changed_fields.append("workflow_visibility")

        # ── Agent channels (the HOW dials) ────────────────────────────
        # Only honoured when the user may edit access AND the org allows
        # the channel. The two are independent of each other and of
        # visibility. A blank/absent value is treated as "off" only when
        # the field was actually submitted, so partial posts (e.g. just a
        # visibility change) don't clobber the other channel.
        can_edit_access = _user_can_edit_workflow_access(request.user, workflow)
        if can_edit_access:
            if "mcp_enabled" in request.POST and getattr(org, "mcp_allowed", False):
                new_mcp = _coerce_bool(request.POST.get("mcp_enabled"))
                if workflow.mcp_enabled != new_mcp:
                    workflow.mcp_enabled = new_mcp
                    changed_fields.append("mcp_enabled")

            if "x402_enabled" in request.POST and getattr(org, "x402_allowed", False):
                new_x402 = _coerce_bool(request.POST.get("x402_enabled"))
                if workflow.x402_enabled != new_x402:
                    workflow.x402_enabled = new_x402
                    changed_fields.append("x402_enabled")

        if changed_fields:
            # ``make_info_page_public`` is always included: the model's
            # ``save()`` auto-publishes the info page for ALL_USERS
            # visibility, and a narrow ``update_fields`` would otherwise
            # drop that synced flag.
            workflow.save(
                update_fields=[*changed_fields, "make_info_page_public", "modified"],
            )

        # Return updated visibility section for HTMX
        context = _build_visibility_section_context(self, workflow, request)
        html = render_to_string(
            "workflows/partials/workflow_visibility_section.html",
            context,
            request=request,
        )
        return HttpResponse(html)


def _coerce_bool(raw) -> bool:
    """Interpret a posted checkbox/boolean string as a Python bool."""
    return (raw or "").strip().lower() in {"true", "1", "on", "yes"}


# RFC 5321 caps an email address at 254 characters; reject anything longer
# before it is stored or rendered.
MAX_INVITE_EMAIL_LENGTH = 254


class WorkflowGuestInviteView(GuestInvitesEnabledMixin, WorkflowObjectMixin, View):
    """
    Invite a guest to access this specific workflow.

    Creates a WorkflowInvite and optionally a notification if the invitee
    is an existing user.

    Gated by ``GuestInvitesEnabledMixin``: when
    ``SiteSettings.allow_guest_invites`` is False, the create endpoint
    returns 403 even for users who otherwise hold ``GUEST_INVITE``
    permission. The site-wide flag is the operator's incident-response
    kill switch and overrides per-org RBAC.
    """

    def get(self, request, *args, **kwargs):
        """Return the invite form modal content."""
        if not self.user_can_manage_sharing():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        workflow = self.get_workflow()
        context = {
            "workflow": workflow,
        }
        return render(
            request,
            "workflows/partials/workflow_guest_invite_form.html",
            context,
        )

    def post(self, request, *args, **kwargs):
        """Process the guest invite form."""
        from validibot.notifications.models import Notification
        from validibot.users.models import User
        from validibot.workflows.models import WorkflowInvite

        if not self.user_can_manage_sharing():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        workflow = self.get_workflow()
        email = (request.POST.get("email") or "").strip().lower()

        if not email:
            messages.error(request, _("Email address is required."))
            return self._render_form_response(request, workflow, email)

        # SECURITY: validate format and cap length before the address is stored
        # on WorkflowInvite.invitee_email and later rendered in notification
        # templates. The previous non-empty-only check accepted payloads like
        # ``<script>...`` (a stored-XSS vector) and unbounded strings.
        if len(email) > MAX_INVITE_EMAIL_LENGTH:
            messages.error(request, _("Email address is too long."))
            return self._render_form_response(request, workflow, email)
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, _("Enter a valid email address."))
            return self._render_form_response(request, workflow, email)

        # Check if user is already a member of the org.  ``Membership.org``
        # has no ``related_name``, so the reverse manager is the default
        # ``membership_set`` — querying ``Membership`` directly is
        # clearer than relying on the reverse name and avoids the
        # AttributeError that ``workflow.org.memberships.filter(...)``
        # used to raise on this path.
        from validibot.users.models import Membership

        existing_membership = Membership.objects.filter(
            org=workflow.org,
            user__email__iexact=email,
            is_active=True,
        ).exists()
        if existing_membership:
            messages.error(
                request,
                _("This user is already a member of the organization."),
            )
            return self._render_form_response(request, workflow, email)

        # Check if user already has access — family-scoped.  A v1
        # grant is family-equivalent to a v2 grant (the permission
        # layer treats both as access to the whole family), so a
        # duplicate scoped only to the v2 row would silently let a
        # manager re-invite a user who already has access via v1.
        from validibot.workflows.models import WorkflowAccessGrant

        existing_grant = WorkflowAccessGrant.objects.filter(
            workflow__org_id=workflow.org_id,
            workflow__slug=workflow.slug,
            user__email__iexact=email,
            is_active=True,
        ).exists()
        if existing_grant:
            messages.error(
                request,
                _("This user already has access to this workflow."),
            )
            return self._render_form_response(request, workflow, email)

        # Check for pending invite — family-scoped (same rationale).
        pending_invite = WorkflowInvite.objects.filter(
            workflow__org_id=workflow.org_id,
            workflow__slug=workflow.slug,
            invitee_email__iexact=email,
            status=WorkflowInvite.Status.PENDING,
        ).exists()
        if pending_invite:
            messages.error(
                request,
                _("An invitation is already pending for this email."),
            )
            return self._render_form_response(request, workflow, email)

        # Find existing user by email
        invitee_user = User.objects.filter(email__iexact=email).first()

        # Create the invite
        # Email is only sent if invitee is NOT already a registered user
        # (registered users receive in-app notifications instead)
        invite = WorkflowInvite.create_with_expiry(
            workflow=workflow,
            inviter=request.user,
            invitee_email=email,
            invitee_user=invitee_user,
            send_email=(invitee_user is None),
        )

        # Create notification if invitee is an existing user
        if invitee_user:
            Notification.objects.create(
                user=invitee_user,
                org=workflow.org,
                type=Notification.Type.WORKFLOW_INVITE,
                workflow_invite=invite,
                payload={
                    "workflow_name": workflow.name,
                    "inviter_name": request.user.name or request.user.email,
                },
            )

        messages.success(
            request,
            _("Invitation sent to %(email)s.") % {"email": email},
        )

        # Return updated guest access section
        return self._render_guest_section_response(request, workflow)

    def _render_form_response(self, request, workflow, email=""):
        """Render the form with errors."""
        context = {
            "workflow": workflow,
            "email": email,
        }
        return render(
            request,
            "workflows/partials/workflow_guest_invite_form.html",
            context,
            status=HTTPStatus.OK,
        )

    def _render_guest_section_response(self, request, workflow):
        """Render the updated guest access section (with HTMX retargeting).

        The shared :func:`_render_family_guest_section` returns the
        bare partial; this wrapper adds the HTMX response headers the
        invite form needs to swap the section back into place and
        close the modal.
        """
        response = _render_family_guest_section(request, workflow, self)
        # Retarget to the guest section (form targets modal content by default)
        response["HX-Retarget"] = "#guest-access-section"
        response["HX-Reswap"] = "outerHTML"
        response["HX-Trigger"] = "close-modal"
        return response


class WorkflowGuestRevokeView(WorkflowObjectMixin, View):
    """Revoke a guest's access to this workflow's *family*.

    Revoking is a family-scoped operation.  Deactivating only the
    v2 row when the user also holds an active grant on v1 leaves
    the user with effective access (the permission layer expands
    grants by family), so the manager sees "revoked" while the
    access is still live.  This view deactivates every active
    grant the user holds in the family.
    """

    def post(self, request, *args, **kwargs):
        from django.db import transaction
        from django.utils import timezone

        from validibot.notifications.models import Notification
        from validibot.workflows.models import WorkflowAccessGrant

        if not self.user_can_manage_sharing():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        workflow = self.get_workflow()
        grant_id = kwargs.get("grant_id")

        # The grant_id may point at any version of the family — the
        # listing dedupes by user, so the displayed row could be the
        # v1 grant even on the v2 sharing page.  Look up by pk +
        # family scope so we accept any in-family pk while still
        # rejecting cross-family attempts (defense against
        # IDOR-style URL tampering).
        display_grant = get_object_or_404(
            WorkflowAccessGrant,
            pk=grant_id,
            workflow__org_id=workflow.org_id,
            workflow__slug=workflow.slug,
            is_active=True,
        )

        # Deactivate every active grant this user holds in the family
        # so the family-grant rule in permissions.py can no longer
        # authorise them via a sibling version.  ``update`` runs at
        # the DB layer in one statement; ``modified`` is set so the
        # UI's last-modified column reflects the revocation.
        with transaction.atomic():
            WorkflowAccessGrant.objects.filter(
                workflow__org_id=workflow.org_id,
                workflow__slug=workflow.slug,
                user=display_grant.user,
                is_active=True,
            ).update(is_active=False, modified=timezone.now())

        # Audit the revocation. The bulk ``update()`` above bypasses
        # ``post_save``, so the GUEST_REVOKED entry is written explicitly
        # here — the guest is identified by id, never by email.
        from validibot.audit.constants import AuditAction
        from validibot.audit.context import get_current_context
        from validibot.audit.services import AuditLogService

        _audit_ctx = get_current_context()
        AuditLogService.record(
            action=AuditAction.GUEST_REVOKED,
            actor=_audit_ctx.actor,
            org=workflow.org,
            target_type="users.User",
            target_id=str(display_grant.user_id),
            target_repr=f"Guest #{display_grant.user_id}",
            metadata={"scope": "workflow_family", "workflow_slug": workflow.slug},
            request_id=_audit_ctx.request_id,
        )

        # Notify the guest
        Notification.objects.create(
            user=display_grant.user,
            org=workflow.org,
            type=Notification.Type.SYSTEM_ALERT,
            payload={
                "action": "access_revoked",
                "workflow_name": workflow.name,
                "changed_by": request.user.id,
                "message": str(
                    _("Your access to '%(workflow)s' has been removed.")
                    % {"workflow": workflow.name}
                ),
            },
        )

        messages.success(
            request,
            _("Access revoked for %(email)s.") % {"email": display_grant.user.email},
        )

        # Return updated guest access section
        return self._render_guest_section_response(request, workflow)

    def _render_guest_section_response(self, request, workflow):
        """Render the updated guest access section."""
        return _render_family_guest_section(request, workflow, self)


class WorkflowInviteCancelView(WorkflowObjectMixin, View):
    """Cancel a pending workflow invite (family-scoped).

    The invite_id may point at an invite rooted on a sibling version
    of the family (the listing dedupes by email).  Look up by pk +
    family scope so we accept any in-family invite while still
    rejecting cross-family pks.
    """

    def post(self, request, *args, **kwargs):
        from validibot.workflows.models import WorkflowInvite

        if not self.user_can_manage_sharing():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        workflow = self.get_workflow()
        invite_id = kwargs.get("invite_id")

        invite = get_object_or_404(
            WorkflowInvite,
            pk=invite_id,
            workflow__org_id=workflow.org_id,
            workflow__slug=workflow.slug,
            status=WorkflowInvite.Status.PENDING,
        )

        invite.cancel()

        messages.success(
            request,
            _("Invitation canceled."),
        )

        # Return updated guest access section
        return self._render_guest_section_response(request, workflow)

    def _render_guest_section_response(self, request, workflow):
        """Render the updated guest access section."""
        return _render_family_guest_section(request, workflow, self)


class WorkflowInviteResendView(WorkflowObjectMixin, View):
    """Resend a workflow invite (creates a new invite with fresh expiry)."""

    def post(self, request, *args, **kwargs):
        from validibot.notifications.models import Notification
        from validibot.workflows.models import WorkflowInvite

        if not self.user_can_manage_sharing():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        workflow = self.get_workflow()
        invite_id = kwargs.get("invite_id")

        # Family-scoped lookup — see WorkflowInviteCancelView for rationale.
        old_invite = get_object_or_404(
            WorkflowInvite,
            pk=invite_id,
            workflow__org_id=workflow.org_id,
            workflow__slug=workflow.slug,
        )

        # Cancel the old invite if still pending
        if old_invite.status == WorkflowInvite.Status.PENDING:
            old_invite.cancel()

        # Create a new invite
        # Email is only sent if invitee is NOT already a registered user
        # (registered users receive in-app notifications instead)
        new_invite = WorkflowInvite.create_with_expiry(
            workflow=workflow,
            inviter=request.user,
            invitee_email=old_invite.invitee_email,
            invitee_user=old_invite.invitee_user,
            send_email=(old_invite.invitee_user is None),
        )

        # Create notification if invitee is an existing user
        if new_invite.invitee_user:
            Notification.objects.create(
                user=new_invite.invitee_user,
                org=workflow.org,
                type=Notification.Type.WORKFLOW_INVITE,
                workflow_invite=new_invite,
                payload={
                    "workflow_name": workflow.name,
                    "inviter_name": request.user.name or request.user.email,
                },
            )

        messages.success(
            request,
            _("Invitation resent."),
        )

        # Return updated guest access section
        return self._render_guest_section_response(request, workflow)

    def _render_guest_section_response(self, request, workflow):
        """Render the updated guest access section."""
        return _render_family_guest_section(request, workflow, self)
