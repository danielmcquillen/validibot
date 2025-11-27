"""
Views for managing organization members.
"""

import json
from typing import Any

from django.contrib import messages
from django.db import models
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView
from django.views.generic.edit import FormView

from simplevalidations.core.utils import reverse_with_org
from simplevalidations.users.constants import RoleCode
from simplevalidations.notifications.models import Notification
from simplevalidations.users.forms import InviteUserForm, OrganizationMemberForm
from simplevalidations.users.forms import OrganizationMemberRolesForm
from simplevalidations.users.mixins import OrganizationAdminRequiredMixin
from simplevalidations.users.models import Membership, PendingInvite, User


class MemberListView(OrganizationAdminRequiredMixin, TemplateView):
    """Display all members for the active organization and provide an add form."""

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
            PendingInvite.objects.filter(org=self.organization).order_by("-created")
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
                    OrganizationMemberForm(organization=self.organization),
                ),
                "invite_form": kwargs.get(
                    "invite_form",
                    InviteUserForm(organization=self.organization, inviter=self.request.user),
                ),
            },
        )
        return context

    def post(self, request, *args, **kwargs):
        form = OrganizationMemberForm(request.POST, organization=self.organization)
        if form.is_valid():
            form.save()
            messages.success(request, _("Member added."))
            return HttpResponseRedirect(self._success_url())
        context = self.get_context_data(add_form=form)
        return self.render_to_response(context, status=400)

    def _success_url(self) -> str:
        return reverse_with_org("members:member_list", request=self.request)


class InviteSearchView(OrganizationAdminRequiredMixin, TemplateView):
    """Return type-ahead search results for inviters."""

    organization_context_attr = "organization"
    template_name = "members/partials/invite_search_results.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("q", "").strip()
        matches: list[User] = []
        if len(query) >= 3:
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


class InviteCreateView(OrganizationAdminRequiredMixin, View):
    """Handle invite creation via type-ahead selection or raw email."""

    organization_context_attr = "organization"

    def post(self, request, *args, **kwargs):
        form = InviteUserForm(
            data=request.POST,
            organization=self.organization,
            inviter=request.user,
        )
        if form.is_valid():
            invite = form.save()
            if invite.invitee_user:
                Notification.objects.create(
                    user=invite.invitee_user,
                    org=invite.org,
                    type=Notification.Type.INVITE,
                    invite=invite,
                    payload={"roles": invite.roles, "inviter": request.user.id},
                )
            messages.success(
                request,
                _("Invitation sent."),
            )
            return HttpResponseRedirect(reverse_with_org("members:member_list", request=request))
        memberships = (
            Membership.objects.filter(org=self.organization, is_active=True)
            .select_related("user")
            .prefetch_related("membership_roles__role")
            .order_by("user__name", "user__username")
        )
        context = {
            "organization": self.organization,
            "memberships": memberships,
            "pending_invites": PendingInvite.objects.filter(org=self.organization).order_by("-created"),
            "add_form": OrganizationMemberForm(organization=self.organization),
            "invite_form": form,
        }
        return render(request, "members/member_list.html", context, status=400)


class InviteCancelView(OrganizationAdminRequiredMixin, View):
    """Allow an inviter to cancel a pending invite."""

    organization_context_attr = "organization"

    def post(self, request, *args, **kwargs):
        invite = get_object_or_404(
            PendingInvite,
            pk=kwargs.get("invite_id"),
            org=self.organization,
            inviter=request.user,
        )
        if invite.status == PendingInvite.Status.PENDING:
            invite.status = PendingInvite.Status.CANCELED
            invite.save(update_fields=["status"])
            messages.info(request, _("Invitation canceled."))
        return HttpResponseRedirect(reverse_with_org("members:member_list", request=request))


class MemberUpdateView(OrganizationAdminRequiredMixin, FormView):
    """
    Allow administrators to toggle role assignments for a member.
    """

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

    def form_valid(self, form):
        form.save()
        messages.success(self.request, _("Member roles updated."))
        return HttpResponseRedirect(self._success_url())

    def _success_url(self) -> str:
        return reverse_with_org("members:member_list", request=self.request)


class MemberDeleteView(OrganizationAdminRequiredMixin, View):
    """Handle member removal while protecting required admin/owner roles."""

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
                "The organization owner cannot be removed. Contact support to transfer ownership."
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
        success_message = _("Member removed.")
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
