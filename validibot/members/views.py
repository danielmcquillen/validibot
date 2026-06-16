"""
Views for managing organization members.
"""

import json
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.db import models
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.shortcuts import resolve_url
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView
from django.views.generic.edit import FormView

from validibot.core.constants import InviteStatus
from validibot.core.features import CommercialFeature
from validibot.core.mixins import BreadcrumbMixin
from validibot.core.mixins import FeatureRequiredMixin
from validibot.core.mixins import GuestInvitesEnabledMixin
from validibot.core.utils import reverse_with_org
from validibot.events.constants import AppEventType
from validibot.notifications.models import Notification
from validibot.tracking.constants import TrackingEventType
from validibot.tracking.services import TrackingEventService
from validibot.users.constants import PermissionCode
from validibot.users.constants import RoleCode
from validibot.users.forms import InviteUserForm
from validibot.users.forms import OrganizationMemberForm
from validibot.users.forms import OrganizationMemberRolesForm
from validibot.users.mixins import OrganizationAdminRequiredMixin
from validibot.users.mixins import OrganizationPermissionRequiredMixin
from validibot.users.models import MemberInvite
from validibot.users.models import Membership
from validibot.users.models import User

# Session key under which an anonymous visitor's member-invite token is
# stashed by ``MemberInviteAcceptView`` and later redeemed by the
# allauth ``AccountAdapter`` after signup completes. The adapter defines
# the same literal (``MEMBER_INVITE_SESSION_KEY``) — the two are kept in
# lockstep, mirroring the guest-invite convention.
MEMBER_INVITE_SESSION_KEY = "member_invite_token"

# Minimum length before the invite type-ahead runs a user search. Below
# this, a 1–2 character ``search`` term would match a huge slice of the
# user table, so we wait until the admin has typed something specific.
INVITE_SEARCH_MIN_QUERY_LENGTH = 3


def user_owns_invited_email(user: User, invited_email: str | None) -> bool:
    """Return True iff *user* provably owns *invited_email*.

    This is the single ownership predicate every email-only invite
    redemption path consults before binding an invite to an account.
    Without it, a *leaked* invite link (forwarded email, shared device,
    guessed token) lets whoever clicks it claim an invitation addressed
    to a different person's email — a cross-account privilege grant.

    Ownership is satisfied when the invited address matches, case-
    insensitively, either:

    * a **verified** allauth :class:`~allauth.account.models.EmailAddress`
      row belonging to ``user`` (the authoritative record of which
      addresses the account has proven control of), or
    * ``user.email`` itself, but only when the account has *no*
      ``EmailAddress`` rows at all. Some accounts (legacy users, fixtures,
      programmatically created members) carry an email on the ``User``
      row without a corresponding verified ``EmailAddress``; treating the
      primary ``User.email`` as owned in that case avoids stranding
      otherwise-legitimate redemptions while never *widening* ownership
      for accounts that do have verified-email records to compare against.

    Args:
        user: The authenticated account attempting to redeem an invite.
        invited_email: The ``invitee_email`` recorded on the invite.

    Returns:
        True when the account owns the invited address; False otherwise
        (including when either side is blank — a blank invite email has no
        owner and must never auto-bind).
    """
    from allauth.account.models import EmailAddress

    if not invited_email:
        return False

    target = invited_email.strip().lower()
    if not target:
        return False

    verified_emails = {
        addr.lower()
        for addr in EmailAddress.objects.filter(
            user=user,
            verified=True,
        ).values_list("email", flat=True)
    }
    if verified_emails:
        return target in verified_emails

    # No verified EmailAddress rows exist for this account — fall back to
    # the primary ``User.email``. This is deliberately the *only* branch
    # that trusts ``User.email`` unaccompanied, so an account that has
    # verified-email records can never bypass them via a stale primary.
    if EmailAddress.objects.filter(user=user).exists():
        # The account has EmailAddress rows but none are verified — it has
        # proven control of nothing, so it owns no invited address.
        return False

    return target == (user.email or "").strip().lower()


def finalize_member_invite_accept(invite: MemberInvite, user: User) -> Membership:
    """Bind, accept, and run the side effects of accepting a member invite.

    Shared by the two non-notification acceptance paths —
    :class:`MemberInviteAcceptView` (logged-in click) and the post-signup
    redemption in :class:`validibot.users.adapters.AccountAdapter` — so
    both create the :class:`~validibot.users.models.Membership`, log the
    ``INVITE_ACCEPTED`` analytics event, and tell the inviter, exactly the
    way the notification-based accept flow does. Keeping it in one place
    stops the three accept surfaces from drifting apart.

    For an email-only invite (``invitee_user is None``) the caller is
    responsible for confirming the account owns the invited email *before*
    calling this; here we simply bind the user and accept.

    Raises:
        SeatQuotaExceededError: If the org is at its seat cap. Callers
            surface this as a friendly flash message rather than a 500 —
            it's a routine "free a seat" moment, not a system failure.
    """
    if invite.invitee_user_id is None:
        invite.invitee_user = user
        invite.save(update_fields=["invitee_user"])

    membership = invite.accept()

    TrackingEventService().log_tracking_event(
        event_type=TrackingEventType.APP_EVENT,
        app_event_type=AppEventType.INVITE_ACCEPTED,
        project=None,
        org=invite.org,
        user=user,
        extra_data={
            "invite_id": str(invite.id),
            "inviter_id": getattr(invite.inviter, "id", None),
            "invitee_user_id": user.id,
            "invitee_email": invite.invitee_email,
            "roles": invite.roles,
        },
        channel="web",
    )

    # Let the inviter know their invitation landed. Mirrors the
    # notification-accept flow's inviter notification so an admin sees the
    # same confirmation regardless of how the invitee accepted.
    if invite.inviter:
        Notification.objects.create(
            user=invite.inviter,
            org=invite.org,
            type=Notification.Type.MEMBER_INVITE,
            member_invite=invite,
            payload={
                "message": str(
                    _("Invitation to '%(who)s' to join %(org)s was accepted.")
                    % {
                        "who": user.name or user.username,
                        "org": invite.org.name,
                    },
                ),
            },
        )

    return membership


def claim_pending_member_invites_for_user(user: User) -> list[MemberInvite]:
    """Bind and surface pending, email-only member invites for *user*.

    Member-invite acceptance normally rides on a browser-session token: the
    invitee clicks the emailed link (which stashes the token) and signs up in
    the *same* browser, where
    :class:`~validibot.users.adapters.AccountAdapter` redeems it. A user who
    obtains an account any other way — signing up directly without the link,
    or *already* having an account when invited by email — never carries that
    token, so the invite is stranded ``PENDING`` with ``invitee_user`` unset
    and the invitee has no in-app way to see it.

    Called at login (see ``validibot.users.signals``), this rescues those
    orphaned invites: for every ``PENDING``, non-expired invite addressed to
    ``user.email`` (case-insensitive) that is not yet bound to an account, it
    binds ``invitee_user`` and creates the *same* ``MEMBER_INVITE``
    notification a user-targeted invite would have received at creation time
    (see :class:`InviteCreateView`). The existing notification UI then offers
    one-click Accept/Decline.

    It deliberately does **not** auto-accept — joining an organization stays
    an explicit choice — and it is idempotent: only invites with no
    ``invitee_user`` are claimed, so repeat logins are no-ops.

    Args:
        user: The freshly-authenticated account.

    Returns:
        The invites newly claimed in this call (for surfacing a message).
    """
    email = (user.email or "").strip()
    if not email:
        return []

    invites = list(
        MemberInvite.objects.select_related("org", "inviter").filter(
            invitee_user__isnull=True,
            invitee_email__iexact=email,
            status=MemberInvite.InviteStatus.PENDING,
            expires_at__gt=timezone.now(),
        ),
    )

    claimed: list[MemberInvite] = []
    for invite in invites:
        invite.invitee_user = user
        invite.save(update_fields=["invitee_user", "modified"])
        # Mirror InviteCreateView's user-targeted notification so the invite
        # appears in the invitee's notification bell with Accept/Decline.
        Notification.objects.create(
            user=user,
            org=invite.org,
            type=Notification.Type.MEMBER_INVITE,
            member_invite=invite,
            payload={"roles": invite.roles, "inviter": invite.inviter_id},
        )
        claimed.append(invite)
    return claimed


class MemberListView(
    FeatureRequiredMixin,
    OrganizationAdminRequiredMixin,
    TemplateView,
):
    """Display all members for the active organization and provide an add form."""

    required_commercial_feature = CommercialFeature.TEAM_MANAGEMENT
    template_name = "members/member_list.html"
    organization_context_attr = "organization"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        memberships = (
            Membership.objects.filter(org=self.organization, is_active=True)
            .select_related("user")
            .prefetch_related("membership_roles__role")
            .order_by("user__name", "user__username")
        )
        pending_invites = MemberInvite.objects.filter(
            org=self.organization,
            status=InviteStatus.PENDING,
            expires_at__gt=timezone.now(),
        ).order_by("-created")
        context.update(
            {
                "organization": self.organization,
                "memberships": memberships,
                "pending_invites": pending_invites,
                "add_form": kwargs.get(
                    "add_form",
                    OrganizationMemberForm(
                        organization=self.organization,
                        request_user=self.request.user,
                    ),
                ),
                "invite_form": kwargs.get(
                    "invite_form",
                    InviteUserForm(
                        organization=self.organization, inviter=self.request.user
                    ),
                ),
            },
        )
        return context

    def post(self, request, *args, **kwargs):
        form = OrganizationMemberForm(
            request.POST,
            organization=self.organization,
            request_user=request.user,
        )
        if form.is_valid():
            from validibot.users.seats import SeatQuotaExceededError

            try:
                form.save()
            except SeatQuotaExceededError as exc:
                # Lost the seat-cap race between the clean()-time check and the
                # locked create (rare: two admins adding the last seat at the
                # same instant). Surface it as a form error rather than a 500.
                form.add_error(None, str(exc))
                context = self.get_context_data(add_form=form)
                return self.render_to_response(context, status=400)
            messages.success(request, _("Member added."))
            return HttpResponseRedirect(self._success_url())
        context = self.get_context_data(add_form=form)
        return self.render_to_response(context, status=400)

    def _success_url(self) -> str:
        return reverse_with_org("members:member_list", request=self.request)


class InviteFormView(
    FeatureRequiredMixin,
    OrganizationAdminRequiredMixin,
    TemplateView,
):
    """Return the member invite form for the modal."""

    required_commercial_feature = CommercialFeature.TEAM_MANAGEMENT
    organization_context_attr = "organization"
    template_name = "members/partials/member_invite_form.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "organization": self.organization,
                "invite_form": InviteUserForm(
                    organization=self.organization,
                    inviter=self.request.user,
                ),
            },
        )
        return context


class InviteSearchView(
    FeatureRequiredMixin,
    OrganizationAdminRequiredMixin,
    TemplateView,
):
    """Return type-ahead search results for inviters."""

    required_commercial_feature = CommercialFeature.TEAM_MANAGEMENT
    organization_context_attr = "organization"
    template_name = "members/partials/invite_search_results.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("search", "").strip()
        matches: list[User] = []
        if len(query) >= INVITE_SEARCH_MIN_QUERY_LENGTH:
            # SECURITY: scope the searchable population to users the admin's
            # org already has a relationship with, never the global user
            # table. The previous query ran an un-org-scoped
            # ``username/email/name`` ``icontains`` across *every* User —
            # an org admin could type fragments and enumerate accounts in
            # unrelated tenants (cross-tenant user enumeration), harvesting
            # emails and usernames that should be invisible to them. We
            # restrict the base queryset to people the org already knows
            # about — anyone the org has invited (so type-ahead still helps
            # re-invite or correct a pending invite) — and exclude current
            # active members (who can't be re-invited). Inviting a truly new
            # outside email goes through the raw-email path in
            # ``InviteCreateView``, which never reveals whether the address
            # belongs to an existing account.
            related_user_ids = MemberInvite.objects.filter(
                org=self.organization,
                invitee_user__isnull=False,
            ).values_list("invitee_user_id", flat=True)

            matches = (
                User.objects.filter(pk__in=related_user_ids)
                .filter(
                    models.Q(username__icontains=query)
                    | models.Q(email__icontains=query)
                    | models.Q(name__icontains=query),
                )
                .exclude(
                    memberships__org=self.organization,
                    memberships__is_active=True,
                )
                .distinct()[:5]
            )
        context.update(
            {
                "query": query,
                "matches": matches,
                "organization": self.organization,
            },
        )
        return context


class InviteConfirmView(FeatureRequiredMixin, OrganizationAdminRequiredMixin, View):
    """Render a confirmation dialog before an invite is actually sent.

    The invite modal posts here first — *not* straight to
    :class:`InviteCreateView`. We validate the very same
    :class:`~validibot.users.forms.InviteUserForm` the create view uses,
    then render an interstitial that spells out *who* is being invited
    and *which* permissions they'll receive, asking the admin to confirm.
    Only the confirmation's "Invite" button posts to ``InviteCreateView``,
    so the create endpoint keeps its existing one-POST-creates contract
    and a misclick can't silently grant organization access.

    The confirmation has two flavours, decided by whether the form
    resolved an existing Validibot account for the target:

    * **Existing user** — show their identity (avatar, name, username,
      email) so the admin can be sure they're inviting the right person
      before turning them into a member.
    * **Brand-new email** — warn that no Validibot account exists yet for
      this address; confirming emails them an invitation to sign up and
      join with the selected permissions.
    """

    required_commercial_feature = CommercialFeature.TEAM_MANAGEMENT
    organization_context_attr = "organization"

    def post(self, request, *args, **kwargs):
        form = InviteUserForm(
            data=request.POST,
            organization=self.organization,
            inviter=request.user,
        )
        if not form.is_valid():
            # Re-render the form with its errors, exactly like the create
            # view does on invalid input, so the modal shows inline
            # validation feedback instead of an empty confirmation.
            return render(
                request,
                "members/partials/member_invite_form.html",
                {"organization": self.organization, "invite_form": form},
            )

        invitee_user = form.cleaned_data.get("invitee_user")
        invitee_email = form.cleaned_data.get("invitee_email")
        # Mirror ``InviteUserForm.save()``'s default so the confirmation
        # lists exactly the roles that will be granted on accept.
        role_codes = form.cleaned_data.get("roles") or [RoleCode.WORKFLOW_VIEWER]
        selected_roles = [
            {"code": code, "label": RoleCode(code).label} for code in role_codes
        ]
        context = {
            "organization": self.organization,
            "invitee_user": invitee_user,
            "invitee_email": invitee_email,
            "selected_roles": selected_roles,
            # Echoed back verbatim as hidden fields so the confirm POST
            # re-binds the same form without re-entering anything.
            "search_value": request.POST.get("search", ""),
            "role_codes": role_codes,
        }
        return render(
            request,
            "members/partials/member_invite_confirm.html",
            context,
        )


class InviteCreateView(FeatureRequiredMixin, OrganizationAdminRequiredMixin, View):
    """Handle invite creation via type-ahead selection or raw email."""

    required_commercial_feature = CommercialFeature.TEAM_MANAGEMENT
    organization_context_attr = "organization"

    def post(self, request, *args, **kwargs):
        from django.http import HttpResponse

        form = InviteUserForm(
            data=request.POST,
            organization=self.organization,
            inviter=request.user,
        )
        if form.is_valid():
            invite = form.save()
            tracking_service = TrackingEventService()
            tracking_service.log_tracking_event(
                event_type=TrackingEventType.APP_EVENT,
                app_event_type=AppEventType.INVITE_CREATED,
                project=None,
                org=invite.org,
                user=request.user,
                extra_data={
                    "invite_id": str(invite.id),
                    "invitee_user_id": getattr(invite.invitee_user, "id", None),
                    "invitee_email": invite.invitee_email,
                    "roles": invite.roles,
                    "status": invite.status,
                },
                channel="web",
            )
            if invite.invitee_user:
                Notification.objects.create(
                    user=invite.invitee_user,
                    org=invite.org,
                    type=Notification.Type.MEMBER_INVITE,
                    member_invite=invite,
                    payload={"roles": invite.roles, "inviter": request.user.id},
                )
            messages.success(
                request,
                _("Invitation sent."),
            )

            redirect_url = reverse_with_org("members:member_list", request=request)

            # For HTMX requests, use HX-Redirect to close modal and redirect
            if request.headers.get("HX-Request"):
                response = HttpResponse()
                response["HX-Redirect"] = redirect_url
                return response

            return HttpResponseRedirect(redirect_url)

        # Form validation failed - re-render the form with errors
        context = {
            "organization": self.organization,
            "invite_form": form,
        }
        return render(
            request,
            "members/partials/member_invite_form.html",
            context,
        )


class InviteCancelView(FeatureRequiredMixin, OrganizationAdminRequiredMixin, View):
    """Allow an inviter to cancel a pending invite."""

    required_commercial_feature = CommercialFeature.TEAM_MANAGEMENT
    organization_context_attr = "organization"

    def post(self, request, *args, **kwargs):
        invite = get_object_or_404(
            MemberInvite,
            pk=kwargs.get("invite_id"),
            org=self.organization,
            inviter=request.user,
        )
        if invite.is_pending:
            invite.cancel()
            messages.info(request, _("Invitation canceled."))
        return HttpResponseRedirect(
            reverse_with_org("members:member_list", request=request)
        )


class MemberInviteAcceptView(View):
    """Accept a membership invite via the tokenized URL we email.

    This is the piece that was missing for brand-new invitees. We only
    email a member invite when ``invitee_user`` is None (an address with
    no Validibot account yet); the email used to point at
    ``/notifications/`` — login-walled and token-less — so the invitee
    had no way to accept. This view mirrors
    :class:`~validibot.workflows.views.sharing.GuestInviteAcceptView`:

    1. **Logged-in users** — accept immediately when the invite is
       addressed to them (or to their email), creating the Membership and
       dropping them into the inviting organization.
    2. **Anonymous users** — stash the token in the session and route to
       signup. After signup the :class:`AccountAdapter` redeems it,
       binding the new account to the invite and creating the Membership.

    Unlike guest invites there is no site-wide kill switch for member
    invites, so this is a plain :class:`~django.views.View`. On
    community deployments no member-invite tokens can be minted in the
    first place (creation is feature-gated), so the token lookup simply
    404s — there's nothing extra to gate here.
    """

    def get(self, request, token):
        invite = get_object_or_404(
            MemberInvite.objects.select_related("org", "inviter"),
            token=token,
        )

        invite.mark_expired_if_needed()
        if not invite.is_pending:
            messages.error(
                request,
                _("This invitation is no longer valid (status: %(status)s).")
                % {"status": invite.get_status_display()},
            )
            return HttpResponseRedirect(reverse("home:home"))

        if not request.user.is_authenticated:
            # Anonymous: stash the token and route through signup. The
            # AccountAdapter consumes the session key after signup
            # completes and redeems the invite on the new account.
            request.session[MEMBER_INVITE_SESSION_KEY] = str(token)
            messages.info(
                request,
                _(
                    "Please sign up or log in to accept your invitation to "
                    "join %(org)s.",
                )
                % {"org": invite.org.name},
            )
            return HttpResponseRedirect(reverse("account_signup"))

        # Logged-in: the invite must be addressed to this account.
        if invite.invitee_user_id and invite.invitee_user_id != request.user.id:
            messages.error(
                request,
                _("This invitation was sent to a different user."),
            )
            return HttpResponseRedirect(reverse("home:home"))
        if invite.invitee_user_id is None:
            # Email-only invite: bind only if the logged-in account
            # provably owns the invited email (case-insensitive match
            # against a verified allauth EmailAddress, or the primary
            # User.email when no EmailAddress records exist). Without this,
            # anyone with the leaked link could redeem an invite addressed
            # to someone else's inbox. Shared with the adapter signup
            # paths via ``user_owns_invited_email`` so every redemption
            # surface enforces the same predicate.
            if not user_owns_invited_email(request.user, invite.invitee_email):
                messages.error(
                    request,
                    _("This invitation is not addressed to your account."),
                )
                return HttpResponseRedirect(reverse("home:home"))

        # Seat-cap refusals on paid editions are raised by
        # ``invite.accept()`` (inside the helper) and surfaced as a flash
        # error rather than a 500 — a routine "ask your admin to free a
        # seat" friction moment.
        from validibot.users.seats import SeatQuotaExceededError

        try:
            finalize_member_invite_accept(invite, request.user)
        except SeatQuotaExceededError as exc:
            messages.error(request, str(exc))
            return HttpResponseRedirect(reverse("home:home"))

        messages.success(
            request,
            _("You're now a member of %(org)s.") % {"org": invite.org.name},
        )
        # Drop them into the organization they just joined.
        request.user.set_current_org(invite.org)
        # ``LOGIN_REDIRECT_URL`` is a URL *name* (``users:redirect``);
        # resolve_url handles both names and paths so this stays correct
        # if the setting ever changes to a literal path.
        return HttpResponseRedirect(resolve_url(settings.LOGIN_REDIRECT_URL))


class MemberUpdateView(
    FeatureRequiredMixin,
    OrganizationAdminRequiredMixin,
    BreadcrumbMixin,
    FormView,
):
    """
    Allow administrators to toggle role assignments for a member.
    """

    required_commercial_feature = CommercialFeature.TEAM_MANAGEMENT
    template_name = "members/member_form.html"
    form_class = OrganizationMemberRolesForm
    organization_context_attr = "organization"

    def dispatch(self, request, *args, **kwargs):
        self.membership = get_object_or_404(
            Membership.objects.select_related("org", "user"),
            pk=kwargs.get("member_id"),
            org=self.get_organization(),
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["membership"] = self.membership
        return kwargs

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "membership": self.membership,
                "organization": self.organization,
            },
        )
        return context

    def get_breadcrumbs(self):
        return [
            {
                "name": _("Members"),
                "url": reverse_with_org("members:member_list", request=self.request),
            },
            {
                "name": str(self.membership.user.name or self.membership.user.username),
                "url": "",
            },
        ]

    def form_valid(self, form):
        form.save()
        messages.success(self.request, _("Member roles updated."))
        return HttpResponseRedirect(self._success_url())

    def _success_url(self) -> str:
        return reverse_with_org("members:member_list", request=self.request)


class MemberDeleteView(FeatureRequiredMixin, OrganizationAdminRequiredMixin, View):
    """Handle member removal while protecting required admin/owner roles."""

    required_commercial_feature = CommercialFeature.TEAM_MANAGEMENT
    organization_context_attr = "organization"

    def post(self, request, *args, **kwargs):
        is_htmx = request.headers.get("HX-Request") == "true"
        membership = get_object_or_404(
            Membership.objects.select_related("org", "user"),
            pk=kwargs.get("member_id"),
            org=self.organization,
        )

        if membership.user_id == request.user.id:
            message = _("You cannot remove yourself.")
            messages.error(request, message)
            if is_htmx:
                return self._render_member_card(
                    request,
                    status=400,
                    toast_level="danger",
                    toast_message=message,
                )
            return HttpResponseRedirect(self._success_url())

        if membership.has_role(RoleCode.OWNER):
            message = _(
                "The organization owner cannot be removed. "
                "Contact support to transfer ownership."
            )
            messages.error(request, message)
            if is_htmx:
                return self._render_member_card(
                    request,
                    status=400,
                    toast_level="danger",
                    toast_message=message,
                )
            return HttpResponseRedirect(self._success_url())

        if not self._can_remove_role(membership, RoleCode.ADMIN):
            message = _("Cannot remove the final administrator from an organization.")
            messages.error(request, message)
            if is_htmx:
                return self._render_member_card(
                    request,
                    status=400,
                    toast_level="danger",
                    toast_message=message,
                )
            return HttpResponseRedirect(self._success_url())

        if not self._can_remove_role(membership, RoleCode.OWNER):
            message = _("Cannot remove the final owner from an organization.")
            messages.error(request, message)
            if is_htmx:
                return self._render_member_card(
                    request,
                    status=400,
                    toast_level="danger",
                    toast_message=message,
                )
            return HttpResponseRedirect(self._success_url())

        membership.delete()
        success_message = self.get_success_message(membership)
        messages.success(request, success_message)
        if is_htmx:
            return self._render_member_card(
                request,
                status=200,
                toast_level="success",
                toast_message=success_message,
            )
        return HttpResponseRedirect(self._success_url())

    def delete(self, request, *args, **kwargs):
        return self.post(request, *args, **kwargs)

    def get_success_message(self, membership: Membership) -> str:
        return _("Member removed.")

    def _can_remove_role(self, membership: Membership, role: str) -> bool:
        if not membership.has_role(role):
            return True
        remaining = (
            Membership.objects.filter(
                org=membership.org,
                is_active=True,
                membership_roles__role__code=role,
            )
            .exclude(pk=membership.pk)
            .distinct()
            .count()
        )
        return remaining > 0

    def _success_url(self) -> str:
        return reverse_with_org("members:member_list", request=self.request)

    def _render_member_card(
        self,
        request,
        *,
        status: int = 200,
        toast_level: str | None = None,
        toast_message: str | None = None,
    ):
        memberships = (
            Membership.objects.filter(org=self.organization, is_active=True)
            .select_related("user")
            .prefetch_related("membership_roles__role")
            .order_by("user__name", "user__username")
        )
        response = render(
            request,
            "members/partials/member_table.html",
            {
                "organization": self.organization,
                "memberships": memberships,
            },
            status=status,
        )
        if toast_level and toast_message:
            response["HX-Trigger"] = json.dumps(
                {
                    "toast": {
                        "level": toast_level,
                        "message": str(toast_message),
                    }
                },
            )
        return response


class MemberDeleteConfirmView(MemberDeleteView, TemplateView):
    """Render a confirmation page before removing a member."""

    template_name = "members/member_delete_confirm.html"

    def get(self, request, *args, **kwargs):
        membership = get_object_or_404(
            Membership.objects.select_related("org", "user"),
            pk=kwargs.get("member_id"),
            org=self.organization,
        )
        return render(
            request,
            self.template_name,
            {
                "membership": membership,
                "organization": self.organization,
            },
        )

    def get_success_message(self, membership: Membership) -> str:
        return _("User '%(username)s' removed from organization") % {
            "username": membership.user.username,
        }


# =============================================================================
# Guest Management Views
# =============================================================================


class GuestListView(
    FeatureRequiredMixin,
    OrganizationAdminRequiredMixin,
    BreadcrumbMixin,
    TemplateView,
):
    """
    Display all guests (users with workflow access but no membership) for the org.

    Note: future iterations should also let Authors access this page
    (scoped to workflows they authored). Currently only Admins/Owners
    have access.
    """

    required_commercial_feature = CommercialFeature.GUEST_MANAGEMENT
    template_name = "members/guest_list.html"
    organization_context_attr = "organization"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        from validibot.workflows.models import GuestInvite
        from validibot.workflows.models import WorkflowAccessGrant

        context = super().get_context_data(**kwargs)

        # Get all users with active grants in this org who are NOT members
        member_user_ids = Membership.objects.filter(
            org=self.organization,
            is_active=True,
        ).values_list("user_id", flat=True)

        # Get grants grouped by user
        grants_by_user = (
            WorkflowAccessGrant.objects.filter(
                workflow__org=self.organization,
                is_active=True,
            )
            .exclude(user_id__in=member_user_ids)
            .select_related("user", "workflow", "granted_by")
            .order_by("user__email", "workflow__name")
        )

        # Group grants by user
        guests: dict = {}
        for grant in grants_by_user:
            user_id = grant.user_id
            if user_id not in guests:
                guests[user_id] = {
                    "user": grant.user,
                    "grants": [],
                    "workflow_count": 0,
                }
            guests[user_id]["grants"].append(grant)
            guests[user_id]["workflow_count"] += 1

        # Get pending guest invites — filter status and expiry in the
        # queryset so we don't need to re-check in Python.
        pending_invites = (
            GuestInvite.objects.filter(
                org=self.organization,
                status=InviteStatus.PENDING,
                expires_at__gt=timezone.now(),
            )
            .select_related("inviter", "invitee_user")
            .prefetch_related("workflows")
            .order_by("-created")
        )

        context.update(
            {
                "organization": self.organization,
                "guests": list(guests.values()),
                "guest_count": len(guests),
                "pending_invites": pending_invites,
            },
        )
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append({"name": _("Guests")})
        return breadcrumbs


class GuestInviteCreateView(
    GuestInvitesEnabledMixin,
    FeatureRequiredMixin,
    OrganizationPermissionRequiredMixin,
    View,
):
    """Create a new org-level guest invite.

    Authority is granted through ``GUEST_INVITE`` (held by ADMIN, AUTHOR,
    and OWNER roles) rather than the broader ``ADMIN_MANAGE_ORG`` —
    authors can send guest invites without admin-level authority. The
    view subclasses :class:`OrganizationPermissionRequiredMixin`
    directly so the permission required is exactly the one the action
    represents.

    The :class:`GuestInvitesEnabledMixin` site-wide kill-switch comes
    first so an operator-flipped flag overrides feature gating and
    per-org RBAC. Order matters here: a 403 from the site-wide gate is
    a more honest answer than a 404 from the feature gate when the
    feature *is* licensed but currently disabled.
    """

    required_commercial_feature = CommercialFeature.GUEST_MANAGEMENT
    organization_context_attr = "organization"
    required_org_permission = PermissionCode.GUEST_INVITE

    def get(self, request, *args, **kwargs):
        """Return the invite form modal content."""
        from validibot.workflows.models import Workflow

        workflows = Workflow.objects.filter(
            org=self.organization,
            is_active=True,
            is_archived=False,
        ).order_by("name")

        context = {
            "organization": self.organization,
            "workflows": workflows,
        }
        return render(
            request,
            "members/partials/guest_invite_form.html",
            context,
        )

    def post(self, request, *args, **kwargs):
        """Process the guest invite form."""
        from validibot.workflows.models import GuestInvite
        from validibot.workflows.models import Workflow

        email = (request.POST.get("email") or "").strip().lower()
        scope = request.POST.get("scope", GuestInvite.Scope.SELECTED)
        workflow_ids = request.POST.getlist("workflows")

        if not email:
            messages.error(request, _("Email address is required."))
            return self._render_form_response(request, email, scope, workflow_ids)

        # Check if user is already a member
        existing_membership = Membership.objects.filter(
            org=self.organization,
            user__email__iexact=email,
            is_active=True,
        ).exists()
        if existing_membership:
            messages.error(
                request,
                _("This user is already a member of the organization."),
            )
            return self._render_form_response(request, email, scope, workflow_ids)

        # Check for pending invite to same email
        pending_invite = GuestInvite.objects.filter(
            org=self.organization,
            invitee_email__iexact=email,
            status=GuestInvite.Status.PENDING,
        ).exists()
        if pending_invite:
            messages.error(
                request,
                _("An invitation is already pending for this email."),
            )
            return self._render_form_response(request, email, scope, workflow_ids)

        # Validate scope and workflows
        if scope == GuestInvite.Scope.SELECTED and not workflow_ids:
            messages.error(
                request,
                _("Please select at least one workflow."),
            )
            return self._render_form_response(request, email, scope, workflow_ids)

        # Find existing user
        invitee_user = User.objects.filter(email__iexact=email).first()

        # Get selected workflows
        workflows = None
        if scope == GuestInvite.Scope.SELECTED:
            workflows = list(
                Workflow.objects.filter(
                    pk__in=workflow_ids,
                    org=self.organization,
                    is_active=True,
                    is_archived=False,
                )
            )

        # Create the invite
        # Email is only sent if invitee is NOT already a registered user
        # (registered users receive in-app notifications instead)
        invite = GuestInvite.create_with_expiry(
            org=self.organization,
            inviter=request.user,
            invitee_email=email,
            invitee_user=invitee_user,
            scope=scope,
            workflows=workflows,
            send_email=(invitee_user is None),
        )

        # Create notification if invitee is an existing user
        if invitee_user:
            Notification.objects.create(
                user=invitee_user,
                org=self.organization,
                type=Notification.Type.GUEST_INVITE,
                guest_invite=invite,
                payload={
                    "org_name": self.organization.name,
                    "inviter_name": request.user.name or request.user.email,
                    "scope": scope,
                },
            )

        messages.success(
            request,
            _("Invitation sent to %(email)s.") % {"email": email},
        )

        # Redirect back to guest list
        redirect_url = reverse_with_org("members:guest_list", request=request)

        # For HTMX requests, use HX-Redirect to close modal and redirect
        if request.headers.get("HX-Request"):
            from django.http import HttpResponse

            response = HttpResponse()
            response["HX-Redirect"] = redirect_url
            return response

        return HttpResponseRedirect(redirect_url)

    def _render_form_response(
        self, request, email="", scope="SELECTED", workflow_ids=None
    ):
        """Render the form with errors."""
        from validibot.workflows.models import Workflow

        workflows = Workflow.objects.filter(
            org=self.organization,
            is_active=True,
            is_archived=False,
        ).order_by("name")

        context = {
            "organization": self.organization,
            "workflows": workflows,
            "email": email,
            "scope": scope,
            "selected_workflow_ids": workflow_ids or [],
        }
        return render(
            request,
            "members/partials/guest_invite_form.html",
            context,
        )


class GuestInviteCancelView(FeatureRequiredMixin, OrganizationAdminRequiredMixin, View):
    """Cancel a pending org-level guest invite."""

    required_commercial_feature = CommercialFeature.GUEST_MANAGEMENT
    organization_context_attr = "organization"

    def post(self, request, *args, **kwargs):
        from validibot.workflows.models import GuestInvite

        invite_id = kwargs.get("invite_id")
        invite = get_object_or_404(
            GuestInvite,
            pk=invite_id,
            org=self.organization,
            status=GuestInvite.Status.PENDING,
        )

        invite.cancel()

        messages.success(request, _("Invitation canceled."))

        # Return to guest list
        return HttpResponseRedirect(
            reverse_with_org("members:guest_list", request=request)
        )


class GuestRevokeAllView(FeatureRequiredMixin, OrganizationAdminRequiredMixin, View):
    """Revoke all workflow access for a guest user."""

    required_commercial_feature = CommercialFeature.GUEST_MANAGEMENT
    organization_context_attr = "organization"

    def post(self, request, *args, **kwargs):
        from validibot.workflows.models import WorkflowAccessGrant

        user_id = kwargs.get("user_id")
        target_user = get_object_or_404(User, pk=user_id)

        # Ensure user is not a member
        is_member = Membership.objects.filter(
            org=self.organization,
            user=target_user,
            is_active=True,
        ).exists()
        if is_member:
            messages.error(
                request,
                _("This user is a member, not a guest. Use member management instead."),
            )
            return HttpResponseRedirect(
                reverse_with_org("members:guest_list", request=request)
            )

        # Revoke all grants
        grants = WorkflowAccessGrant.objects.filter(
            workflow__org=self.organization,
            user=target_user,
            is_active=True,
        )
        revoked_count = grants.update(is_active=False)

        # Audit the revocation. The bulk ``update()`` bypasses
        # ``post_save``, so the GUEST_REVOKED entry is recorded explicitly,
        # identifying the guest by id (never email).
        if revoked_count:
            from validibot.audit.constants import AuditAction
            from validibot.audit.context import get_current_context
            from validibot.audit.services import AuditLogService

            _audit_ctx = get_current_context()
            AuditLogService.record(
                action=AuditAction.GUEST_REVOKED,
                actor=_audit_ctx.actor,
                org=self.organization,
                target_type="users.User",
                target_id=str(target_user.pk),
                target_repr=f"Guest #{target_user.pk}",
                metadata={
                    "scope": "all_workflows",
                    "grants_revoked": revoked_count,
                },
                request_id=_audit_ctx.request_id,
            )

        # Notify the user
        if revoked_count > 0:
            Notification.objects.create(
                user=target_user,
                org=self.organization,
                type=Notification.Type.SYSTEM_ALERT,
                payload={
                    "action": "all_access_revoked",
                    "org_name": self.organization.name,
                    "changed_by": request.user.id,
                    "message": str(
                        _("Your guest access to %(org)s has been removed.")
                        % {"org": self.organization.name}
                    ),
                },
            )

        messages.success(
            request,
            _("Revoked access to %(count)d workflow(s) for %(email)s.")
            % {"count": revoked_count, "email": target_user.email},
        )

        return HttpResponseRedirect(
            reverse_with_org("members:guest_list", request=request)
        )
