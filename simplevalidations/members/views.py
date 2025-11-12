"""
Views for managing organization members.
"""

from typing import Any

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView
from django.views.generic.edit import FormView

from simplevalidations.core.utils import reverse_with_org
from simplevalidations.users.forms import OrganizationMemberForm
from simplevalidations.users.forms import OrganizationMemberRolesForm
from simplevalidations.users.mixins import OrganizationAdminRequiredMixin
from simplevalidations.users.models import Membership
from simplevalidations.users.constants import RoleCode


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
        membership = get_object_or_404(
            Membership.objects.select_related("org", "user"),
            pk=kwargs.get("member_id"),
            org=self.organization,
        )

        if membership.user_id == request.user.id:
            messages.error(request, _("You cannot remove yourself."))
            return HttpResponseRedirect(self._success_url())

        if not self._can_remove_role(membership, RoleCode.ADMIN):
            messages.error(
                request,
                _("Cannot remove the final administrator from an organization."),
            )
            return HttpResponseRedirect(self._success_url())

        if not self._can_remove_role(membership, RoleCode.OWNER):
            messages.error(
                request,
                _("Cannot remove the final owner from an organization."),
            )
            return HttpResponseRedirect(self._success_url())

        membership.delete()
        messages.success(request, _("Member removed."))
        return HttpResponseRedirect(self._success_url())

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
