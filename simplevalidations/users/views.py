from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db.models import QuerySet
from django.urls import reverse
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView
from django.views.generic import RedirectView
from django.views.generic import UpdateView

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
                {"name": _("Profile"), "url": reverse("users:update")},
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
    extra_context = {"active_settings_tab": "profile"}

    def get_success_url(self) -> str:
        assert self.request.user.is_authenticated
        return reverse_lazy(
            "users:detail",
            kwargs={"username": self.request.user.username},
        )

    def get_breadcrumbs(self):
        return [
            {"name": _("Account Settings"), "url": reverse("users:update")},
            {"name": _("Profile"), "url": ""},
        ]

    def get_object(self, queryset: QuerySet | None = None) -> User:
        assert self.request.user.is_authenticated  # type guard
        return self.request.user


user_update_view = UserUpdateView.as_view()


class UserRedirectView(LoginRequiredMixin, RedirectView):
    permanent = False

    def get_redirect_url(self) -> str:
        assert self.request.user.is_authenticated
        return reverse(
            "users:detail",
            kwargs={"username": self.request.user.username},
        )


user_redirect_view = UserRedirectView.as_view()
