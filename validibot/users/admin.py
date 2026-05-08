from allauth.account.decorators import secure_admin_login
from django.conf import settings
from django.contrib import admin
from django.contrib import messages
from django.contrib.auth import admin as auth_admin
from django.utils.translation import gettext_lazy as _

from validibot.users.forms import UserAdminChangeForm
from validibot.users.forms import UserAdminCreationForm
from validibot.users.management.commands.promote_user import demote_user_to_guest
from validibot.users.management.commands.promote_user import promote_user_to_basic
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
    actions = ["promote_to_basic_action", "demote_to_guest_action"]

    @admin.action(description=_("Promote selected users to Basic"))
    def promote_to_basic_action(self, request, queryset):
        """Run :func:`promote_user_to_basic` for each selected user.

        Wrapped on the admin changelist so superusers can promote from
        the browser without shelling into a host. Delegates to the
        same code path as the management command, so the audit log,
        personal-org provisioning, and atomicity guarantees are
        identical regardless of how the operation was invoked.
        """

        if not request.user.is_superuser:
            self.message_user(
                request,
                _("Only superusers can promote users."),
                level=messages.ERROR,
            )
            return

        promoted = 0
        for target in queryset:
            try:
                promote_user_to_basic(target=target, actor=request.user)
                promoted += 1
            except Exception as exc:  # pragma: no cover - admin error path
                self.message_user(
                    request,
                    f"Failed to promote {target.email}: {exc}",
                    level=messages.ERROR,
                )

        self.message_user(
            request,
            _("Promoted %(count)d user(s) to BASIC.") % {"count": promoted},
            level=messages.SUCCESS,
        )

    @admin.action(description=_("Demote selected users to Guest"))
    def demote_to_guest_action(self, request, queryset):
        """Run :func:`demote_user_to_guest` for each selected user.

        Demotion is destructive (it strips member-level capabilities).
        The admin action does not require a per-row confirm flag —
        Django admin's standard "Are you sure you want to perform this
        action on the N selected users?" intermediate page provides
        the guard. The CLI ``--confirm`` flag fills the same role for
        the management command.
        """

        if not request.user.is_superuser:
            self.message_user(
                request,
                _("Only superusers can demote users."),
                level=messages.ERROR,
            )
            return

        demoted = 0
        for target in queryset:
            try:
                demote_user_to_guest(target=target, actor=request.user)
                demoted += 1
            except Exception as exc:  # pragma: no cover - admin error path
                self.message_user(
                    request,
                    f"Failed to demote {target.email}: {exc}",
                    level=messages.ERROR,
                )

        self.message_user(
            request,
            _("Demoted %(count)d user(s) to GUEST.") % {"count": demoted},
            level=messages.SUCCESS,
        )

    def get_form(self, request, obj=None, **kwargs):
        """Hide the ``groups`` field from non-superusers.

        The ``Basic Users`` and ``Guests`` Django Groups are the
        classifier-of-record for whether an account is a regular user or
        a guest. Letting any staff user flip group membership from the
        change form would bypass the ``promote_user`` command's
        atomic-transaction-with-audit guarantees. Only superusers can
        edit the groups field; everyone else sees it as read-only.

        Implemented by disabling the form field rather than removing it
        from ``fieldsets`` so the existing layout stays consistent
        across roles — staff still see what groups the user is in, they
        just can't change them.
        """

        form = super().get_form(request, obj, **kwargs)
        if not request.user.is_superuser and "groups" in form.base_fields:
            form.base_fields["groups"].disabled = True
        return form


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
