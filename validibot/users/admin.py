from allauth.account.decorators import secure_admin_login
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import admin as auth_admin
from django.utils.translation import gettext_lazy as _

from validibot.users.forms import UserAdminChangeForm
from validibot.users.forms import UserAdminCreationForm
from validibot.users.models import Membership
from validibot.users.models import MembershipRole
from validibot.users.models import Organization
from validibot.users.models import Role
from validibot.users.models import User

if settings.DJANGO_ADMIN_FORCE_ALLAUTH:
    # Force the `admin` sign in process to go through the `django-allauth` workflow:
    # https://docs.allauth.org/en/latest/common/admin.html#admin
    admin.autodiscover()
    admin.site.login = secure_admin_login(admin.site.login)  # type: ignore[method-assign]


@admin.register(User)
class UserAdmin(auth_admin.UserAdmin):
    form = UserAdminChangeForm
    add_form = UserAdminCreationForm
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (
            _("Personal info"),
            {
                "fields": (
                    "name",
                    "email",
                    "job_title",
                    "company",
                    "location",
                    "timezone",
                    "bio",
                    "avatar",
                ),
            },
        ),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
            },
        ),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
    )
    list_display = ["username", "name", "job_title", "is_superuser"]
    search_fields = ["name", "username", "email", "company"]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "org", "is_active"]
    list_filter = ["is_active", "roles"]
    search_fields = ["user__username", "user__name", "org__name"]


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "slug",
        "is_personal",
        "trial_ends_at",
        "trial_duration_days",
    ]
    list_filter = ["is_personal"]
    search_fields = ["name", "slug"]


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ["name", "code"]
    search_fields = ["name", "code"]


@admin.register(MembershipRole)
class MembershipRoleAdmin(admin.ModelAdmin):
    list_display = [
        "membership__user__username",
        "membership__org__name",
        "role__name",
        "role__code",
    ]
    search_fields = [
        "membership__user__username",
        "membership__org__name",
        "role__name",
        "role__code",
    ]
