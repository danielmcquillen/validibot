"""
Views for managing organization members.
"""

import json
from typing import Any

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView
from django.views.generic.edit import FormView

from simplevalidations.core.utils import reverse_with_org
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.forms import OrganizationMemberForm
from simplevalidations.users.forms import OrganizationMemberRolesForm
from simplevalidations.users.mixins import OrganizationAdminRequiredMixin
from simplevalidations.users.models import Membership


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
        context.update(
            {
                "organization": self.organization,
                "memberships": memberships,
                "add_form": kwargs.get(
                    "add_form",
                    OrganizationMemberForm(organization=self.organization),
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
