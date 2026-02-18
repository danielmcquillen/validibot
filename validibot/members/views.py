"""
Views for managing organization members.
"""

import json
from typing import Any

from django.contrib import messages
from django.db import models
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView
from django.views.generic.edit import FormView

from validibot.core.features import CommercialFeature
from validibot.core.mixins import BreadcrumbMixin
from validibot.core.mixins import FeatureRequiredMixin
from validibot.core.utils import reverse_with_org
from validibot.events.constants import AppEventType
from validibot.notifications.models import Notification
from validibot.tracking.constants import TrackingEventType
from validibot.tracking.services import TrackingEventService
from validibot.users.constants import RoleCode
from validibot.users.forms import InviteUserForm
from validibot.users.forms import OrganizationMemberForm
from validibot.users.forms import OrganizationMemberRolesForm
from validibot.users.mixins import OrganizationAdminRequiredMixin
from validibot.users.models import MemberInvite
from validibot.users.models import Membership
from validibot.users.models import User


class MemberListView(
    FeatureRequiredMixin,
    OrganizationAdminRequiredMixin,
    TemplateView,
):
    """Display all members for the active organization and provide an add form."""

    required_feature = CommercialFeature.TEAM_MANAGEMENT
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
        pending_invites = list(
            MemberInvite.objects.filter(org=self.organization).order_by("-created")
        )
        for invite in pending_invites:
            invite.mark_expired_if_needed()
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
            form.save()
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

    required_feature = CommercialFeature.TEAM_MANAGEMENT
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

    required_feature = CommercialFeature.TEAM_MANAGEMENT
    organization_context_attr = "organization"
    template_name = "members/partials/invite_search_results.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("search", "").strip()
        matches: list[User] = []
        if len(query) >= 3:  # noqa: PLR2004
            matches = (
                User.objects.filter(
                    models.Q(username__icontains=query)
                    | models.Q(email__icontains=query)
                    | models.Q(name__icontains=query)
                )
                .exclude(memberships__org=self.organization)
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


class InviteCreateView(FeatureRequiredMixin, OrganizationAdminRequiredMixin, View):
    """Handle invite creation via type-ahead selection or raw email."""

    required_feature = CommercialFeature.TEAM_MANAGEMENT
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

    required_feature = CommercialFeature.TEAM_MANAGEMENT
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


class MemberUpdateView(
    FeatureRequiredMixin,
    OrganizationAdminRequiredMixin,
    BreadcrumbMixin,
    FormView,
):
    """
    Allow administrators to toggle role assignments for a member.
    """

    required_feature = CommercialFeature.TEAM_MANAGEMENT
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

    required_feature = CommercialFeature.TEAM_MANAGEMENT
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

    Note: ADR specifies that Authors should also be able to access this page
    (scoped to workflows they authored). Currently only Admins/Owners have
    access. See ADR section 9 for future author permission implementation.
    """

    required_feature = CommercialFeature.TEAM_MANAGEMENT
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

        # Get pending guest invites
        pending_invites = (
            GuestInvite.objects.filter(
                org=self.organization,
                status=GuestInvite.Status.PENDING,
            )
            .select_related("inviter", "invitee_user")
            .prefetch_related("workflows")
            .order_by("-created")
        )

        # Mark expired invites
        for invite in pending_invites:
            invite.mark_expired_if_needed()

        context.update(
            {
                "organization": self.organization,
                "guests": list(guests.values()),
                "guest_count": len(guests),
                "pending_invites": [
                    inv
                    for inv in pending_invites
                    if inv.status == GuestInvite.Status.PENDING
                ],
            },
        )
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append({"name": _("Guests")})
        return breadcrumbs


class GuestInviteCreateView(FeatureRequiredMixin, OrganizationAdminRequiredMixin, View):
    """Create a new org-level guest invite."""

    required_feature = CommercialFeature.TEAM_MANAGEMENT
    organization_context_attr = "organization"

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

    required_feature = CommercialFeature.TEAM_MANAGEMENT
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

    required_feature = CommercialFeature.TEAM_MANAGEMENT
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
