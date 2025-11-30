from allauth.account.views import EmailView as AllauthEmailView
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import CreateView
from django.views.generic import DeleteView
from django.views.generic import DetailView
from django.views.generic import FormView
from django.views.generic import RedirectView
from django.views.generic import TemplateView
from django.views.generic import UpdateView
from rest_framework.authtoken.models import Token

from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.core.utils import reverse_with_org
from simplevalidations.users.constants import PermissionCode
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.forms import OrganizationForm
from simplevalidations.users.forms import OrganizationMemberRolesForm
from simplevalidations.users.forms import UserProfileForm
from simplevalidations.users.mixins import OrganizationAdminRequiredMixin
from simplevalidations.users.models import Membership
from simplevalidations.users.models import Organization
from simplevalidations.users.models import User
from simplevalidations.users.models import ensure_default_project


class UserDetailView(BreadcrumbMixin, LoginRequiredMixin, DetailView):
    model = User
    slug_field = "username"
    slug_url_kwarg = "username"
    template_name = "users/user_detail.html"

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        user_obj = self.get_object()
        if self.request.user == user_obj:
            breadcrumbs.append(
                {
                    "name": _("Profile"),
                    "url": reverse_with_org("users:profile", request=self.request),
                },
            )
            breadcrumbs.append({"name": _("Overview"), "url": ""})
        else:
            breadcrumbs.append({"name": user_obj.username, "url": ""})
        return breadcrumbs


user_detail_view = UserDetailView.as_view()


class UserUpdateView(
    BreadcrumbMixin,
    LoginRequiredMixin,
    SuccessMessageMixin,
    UpdateView,
):
    model = User
    form_class = UserProfileForm
    template_name = "users/profile.html"
    success_message = _("Profile updated successfully")

    def get_success_url(self) -> str:
        assert self.request.user.is_authenticated
        return reverse_with_org(
            "users:detail",
            request=self.request,
            kwargs={"username": self.request.user.username},
        )

    def get_breadcrumbs(self):
        return [
            {
                "name": _("User Settings"),
                "url": reverse_with_org("users:profile", request=self.request),
            },
            {"name": _("Profile"), "url": ""},
        ]

    def get_object(self, queryset: QuerySet | None = None) -> User:
        assert self.request.user.is_authenticated  # type guard
        return self.request.user


user_profile_view = UserUpdateView.as_view()


class UserRedirectView(LoginRequiredMixin, RedirectView):
    permanent = False

    def get_redirect_url(self) -> str:
        assert self.request.user.is_authenticated
        return reverse_with_org(
            "users:detail",
            request=self.request,
            kwargs={"username": self.request.user.username},
        )


user_redirect_view = UserRedirectView.as_view()


@login_required
@require_POST
def user_api_key_rotate_view(request):
    """Regenerate the authenticated user's API token."""

    Token.objects.filter(user=request.user).delete()
    token = Token.objects.create(user=request.user)

    if request.headers.get("HX-Request"):
        response = render(
            request,
            "users/partial/api_key_panel.html",
            {"api_token": token},
        )
        response["HX-Trigger"] = "apiKeyRotated"
        return response

    messages.success(request, _("Generated a new API key."))
    return HttpResponseRedirect(reverse_with_org("users:api-key", request=request))


class UserEmailView(
    BreadcrumbMixin,
    LoginRequiredMixin,
    AllauthEmailView,
):
    template_name = "account/email.html"

    def get_success_url(self):
        return reverse_with_org("users:email", request=self.request)

    def get_breadcrumbs(self):
        return [
            {
                "name": _("User Settings"),
                "url": reverse_with_org("users:profile", request=self.request),
            },
            {"name": _("Email"), "url": ""},
        ]


user_email_view = UserEmailView.as_view()


class UserApiKeyView(
    BreadcrumbMixin,
    LoginRequiredMixin,
    TemplateView,
):
    template_name = "users/api_key.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        token, _ = Token.objects.get_or_create(user=self.request.user)
        context["api_token"] = token
        return context

    def get_breadcrumbs(self):
        return [
            {
                "name": _("User Settings"),
                "url": reverse_with_org("users:profile", request=self.request),
            },
            {"name": _("API Key"), "url": ""},
        ]


user_api_key_view = UserApiKeyView.as_view()


@login_required
@require_POST
def switch_current_org_view(request, org_id: int) -> HttpResponse:
    """
    Switch the logged-in user's active organization and redirect safely.

    Args:
        request: The active HttpRequest.
        org_id: Primary key of the organization being selected.

    Raises:
        PermissionDenied: If the user is not an active member of the org.

    Returns:
        HttpResponse: Redirect (or HX-Redirect) to either the requested
            next URL or the dashboard fallback when the request target is unsafe.
    """
    organization = get_object_or_404(Organization, pk=org_id)
    membership = (
        request.user.memberships.filter(org=organization, is_active=True)
        .select_related("org")
        .first()
    )
    if membership is None:
        raise PermissionDenied("You do not belong to this organization.")

    request.user.set_current_org(organization)
    request.session["active_org_id"] = organization.id
    default_next_url = reverse_with_org("dashboard:my_dashboard", request=request)
    requested_next = request.POST.get("next") or request.GET.get("next")
    if requested_next and url_has_allowed_host_and_scheme(
        url=requested_next,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = requested_next
    else:
        next_url = default_next_url

    if request.headers.get("HX-Request"):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = next_url
        return response

    return redirect(next_url)


def _admin_memberships_for(user: User) -> list[Membership]:
    if not user.is_authenticated:
        return []
    memberships = (
        user.memberships.filter(is_active=True)
        .select_related("org")
        .prefetch_related("membership_roles__role")
    )
    admin_memberships: list[Membership] = []
    for membership in memberships:
        organization = membership.org
        if organization and user.has_perm(
            PermissionCode.ADMIN_MANAGE_ORG.value,
            organization,
        ):
            admin_memberships.append(membership)
    return admin_memberships


class OrganizationListView(BreadcrumbMixin, LoginRequiredMixin, TemplateView):
    template_name = "users/organizations/organization_list.html"
    breadcrumbs = [{"name": _("Organizations"), "url": ""}]

    def dispatch(self, request, *args, **kwargs):
        if not _admin_memberships_for(request.user):
            raise PermissionDenied("Administrator access required.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        memberships = _admin_memberships_for(self.request.user)
        rows = [
            {
                "membership": membership,
                "member_count": Membership.objects.filter(
                    org=membership.org, is_active=True
                ).count(),
            }
            for membership in memberships
        ]
        context.update(
            {
                "admin_rows": rows,
                "create_url": reverse_with_org(
                    "users:organization-create",
                    request=self.request,
                ),
            }
        )
        return context


class OrganizationCreateView(
    BreadcrumbMixin,
    LoginRequiredMixin,
    SuccessMessageMixin,
    CreateView,
):
    model = Organization
    form_class = OrganizationForm
    template_name = "users/organizations/organization_form.html"
    success_message = _("Organization created.")

    def dispatch(self, request, *args, **kwargs):
        if not _admin_memberships_for(request.user):
            raise PermissionDenied("Administrator access required.")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        membership, _ = Membership.objects.get_or_create(
            user=self.request.user,
            org=self.object,
            defaults={"is_active": True},
        )
        membership.set_roles({RoleCode.ADMIN, RoleCode.OWNER, RoleCode.EXECUTOR})
        ensure_default_project(self.object)
        self.request.user.set_current_org(self.object)
        self.request.session["active_org_id"] = self.object.id
        return response

    def get_success_url(self):
        return reverse_with_org("users:organization-list", request=self.request)

    def get_breadcrumbs(self):
        return [
            {
                "name": _("Organizations"),
                "url": reverse_with_org(
                    "users:organization-list", request=self.request
                ),
            },
            {"name": _("New"), "url": ""},
        ]


class OrganizationUpdateView(
    OrganizationAdminRequiredMixin,
    SuccessMessageMixin,
    UpdateView,
):
    organization_context_attr = "organization"
    model = Organization
    form_class = OrganizationForm
    template_name = "users/organizations/organization_form.html"
    success_message = _("Organization updated.")

    def get_breadcrumbs(self):
        organization = getattr(self, "organization", None) or self.get_object()
        return [
            {
                "name": _("Organizations"),
                "url": reverse_with_org(
                    "users:organization-list", request=self.request
                ),
            },
            {
                "name": organization.name,
                "url": reverse_with_org(
                    "users:organization-detail",
                    request=self.request,
                    kwargs={"pk": organization.pk},
                ),
            },
            {"name": _("Edit"), "url": ""},
        ]

    def get_success_url(self):
        return reverse_with_org(
            "users:organization-detail",
            request=self.request,
            kwargs={"pk": self.object.pk},
        )


class OrganizationDeleteView(
    OrganizationAdminRequiredMixin,
    SuccessMessageMixin,
    DeleteView,
):
    organization_context_attr = "organization"
    model = Organization
    template_name = "users/organizations/organization_confirm_delete.html"
    success_message = _("Organization deleted.")

    def get_breadcrumbs(self):
        organization = getattr(self, "organization", None) or self.get_object()
        return [
            {
                "name": _("Organizations"),
                "url": reverse_with_org(
                    "users:organization-list", request=self.request
                ),
            },
            {
                "name": organization.name,
                "url": reverse_with_org(
                    "users:organization-detail",
                    request=self.request,
                    kwargs={"pk": organization.pk},
                ),
            },
            {"name": _("Delete"), "url": ""},
        ]

    def post(self, request, *args, **kwargs):
        organization = self.get_object()
        if organization.is_personal:
            messages.error(request, _("Personal workspaces cannot be deleted."))
            return HttpResponseRedirect(
                reverse_with_org(
                    "users:organization-detail",
                    request=request,
                    kwargs={"pk": organization.pk},
                )
            )
        admin_user_ids = list(
            Membership.objects.filter(
                org=organization,
                is_active=True,
                membership_roles__role__code=RoleCode.ADMIN,
            )
            .values_list("user_id", flat=True)
            .distinct()
        )

        if len(admin_user_ids) <= 1:
            messages.error(
                request,
                _(
                    "You must assign another administrator before "
                    "deleting this organization."
                ),
            )
            return redirect(
                reverse_with_org(
                    "users:organization-detail",
                    request=request,
                    kwargs={"pk": organization.pk},
                )
            )

        remaining_admin_memberships = [
            membership
            for membership in _admin_memberships_for(request.user)
            if membership.org_id != organization.id
        ]
        if remaining_admin_memberships:
            next_org = remaining_admin_memberships[0].org
            request.user.set_current_org(next_org)
            request.session["active_org_id"] = next_org.id
        else:
            request.session.pop("active_org_id", None)
            request.user.current_org = None
            request.user.save(update_fields=["current_org"])

        organization.delete()
        messages.success(request, self.success_message)
        return redirect(reverse_with_org("users:organization-list", request=request))

    def get_success_url(self):
        return reverse_with_org("users:organization-list", request=self.request)


class OrganizationDetailView(
    OrganizationAdminRequiredMixin, BreadcrumbMixin, TemplateView
):
    organization_context_attr = "organization"
    template_name = "users/organizations/organization_detail.html"

    def get_breadcrumbs(self):
        organization = getattr(self, "organization", None)
        return [
            {
                "name": _("Organizations"),
                "url": reverse_with_org(
                    "users:organization-list", request=self.request
                ),
            },
            {"name": organization.name, "url": ""},
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.organization
        member_count = Membership.objects.filter(
            org=organization,
            is_active=True,
        ).count()
        context.update(
            {
                "organization": organization,
                "member_count": member_count,
            }
        )
        return context


class OrganizationMemberRolesUpdateView(OrganizationAdminRequiredMixin, FormView):
    organization_context_attr = "organization"
    form_class = OrganizationMemberRolesForm
    template_name = "users/organizations/organization_member_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.membership = get_object_or_404(
            Membership,
            pk=kwargs.get("member_id"),
            org=self.get_organization(),
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["membership"] = self.membership
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "organization": self.organization,
                "membership": self.membership,
            }
        )
        return context

    def form_valid(self, form):
        organization = self.organization
        new_roles = set(form.cleaned_data.get("roles") or [])
        if RoleCode.ADMIN not in new_roles:
            remaining_admins = (
                Membership.objects.filter(
                    org=organization,
                    is_active=True,
                    membership_roles__role__code=RoleCode.ADMIN,
                )
                .exclude(pk=self.membership.pk)
                .distinct()
                .count()
            )
            if remaining_admins == 0:
                form.add_error(
                    None,
                    _("An organization must retain at least one administrator."),
                )
                return self.form_invalid(form)

        if RoleCode.OWNER not in new_roles:
            remaining_owners = (
                Membership.objects.filter(
                    org=organization,
                    is_active=True,
                    membership_roles__role__code=RoleCode.OWNER,
                )
                .exclude(pk=self.membership.pk)
                .distinct()
                .count()
            )
            if remaining_owners == 0:
                form.add_error(
                    None,
                    _("An organization must retain at least one owner."),
                )
                return self.form_invalid(form)

        form.save()
        messages.success(self.request, _("Roles updated."))
        return redirect(
            reverse_with_org(
                "users:organization-detail",
                request=self.request,
                kwargs={"pk": organization.pk},
            )
        )


class OrganizationMemberDeleteView(OrganizationAdminRequiredMixin, View):
    organization_context_attr = "organization"

    def post(self, request, *args, **kwargs):
        organization = self.organization
        membership = get_object_or_404(
            Membership,
            pk=kwargs.get("member_id"),
            org=organization,
        )

        if membership.user_id == request.user.id:
            messages.error(request, _("You cannot remove yourself."))
            return redirect(
                reverse_with_org(
                    "users:organization-detail",
                    request=request,
                    kwargs={"pk": organization.pk},
                )
            )

        if membership.has_role(RoleCode.OWNER):
            messages.error(
                request,
                _(
                    "The organization owner cannot be removed. "
                    "Contact support to transfer ownership."
                ),
            )
            return redirect(
                reverse_with_org(
                    "users:organization-detail",
                    request=request,
                    kwargs={"pk": organization.pk},
                )
            )

        if membership.is_admin:
            remaining_admins = (
                Membership.objects.filter(
                    org=organization,
                    is_active=True,
                    membership_roles__role__code=RoleCode.ADMIN,
                )
                .exclude(pk=membership.pk)
                .distinct()
                .count()
            )
            if remaining_admins == 0:
                messages.error(
                    request,
                    _("Cannot remove the final administrator from an organization."),
                )
                return redirect(
                    reverse_with_org(
                        "users:organization-detail",
                        request=request,
                        kwargs={"pk": organization.pk},
                    )
                )

        if membership.has_role(RoleCode.OWNER):
            remaining_owners = (
                Membership.objects.filter(
                    org=organization,
                    is_active=True,
                    membership_roles__role__code=RoleCode.OWNER,
                )
                .exclude(pk=membership.pk)
                .distinct()
                .count()
            )
            if remaining_owners == 0:
                messages.error(
                    request,
                    _("Cannot remove the final owner from an organization."),
                )
                return redirect(
                    reverse_with_org(
                        "users:organization-detail",
                        request=request,
                        kwargs={"pk": organization.pk},
                    )
                )

        membership.delete()
        messages.success(request, _("Member removed."))
        return redirect(
            reverse_with_org(
                "users:organization-detail",
                request=request,
                kwargs={"pk": organization.pk},
            )
        )
