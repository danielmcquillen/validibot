from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db.models import QuerySet
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST
from django.views.generic import DetailView
from django.views.generic import RedirectView
from django.views.generic import TemplateView
from django.views.generic import UpdateView

from allauth.account.views import EmailView as AllauthEmailView
from rest_framework.authtoken.models import Token

from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.users.forms import UserProfileForm
from simplevalidations.users.models import User


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
                {"name": _("Profile"), "url": reverse("users:profile")},
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
        return reverse_lazy(
            "users:detail",
            kwargs={"username": self.request.user.username},
        )

    def get_breadcrumbs(self):
        return [
            {"name": _("User Settings"), "url": reverse("users:profile")},
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
        return reverse(
            "users:detail",
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
    return HttpResponseRedirect(reverse("users:api-key"))


class UserEmailView(
    BreadcrumbMixin,
    LoginRequiredMixin,
    AllauthEmailView,
):
    template_name = "account/email.html"

    def get_success_url(self):
        return reverse("users:email")

    def get_breadcrumbs(self):
        return [
            {"name": _("User Settings"), "url": reverse("users:profile")},
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
            {"name": _("User Settings"), "url": reverse("users:profile")},
            {"name": _("API Key"), "url": ""},
        ]


user_api_key_view = UserApiKeyView.as_view()
