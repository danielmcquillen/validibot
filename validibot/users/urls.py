from django.urls import path

from validibot.users.views import OrganizationCreateView
from validibot.users.views import OrganizationDeleteView
from validibot.users.views import OrganizationDetailView
from validibot.users.views import OrganizationListView
from validibot.users.views import OrganizationMemberDeleteView
from validibot.users.views import OrganizationMemberRolesUpdateView
from validibot.users.views import OrganizationUpdateView
from validibot.users.views import switch_current_org_view
from validibot.users.views import user_api_key_rotate_view
from validibot.users.views import user_api_key_view
from validibot.users.views import user_detail_view
from validibot.users.views import user_email_view
from validibot.users.views import user_profile_view
from validibot.users.views import user_redirect_view

app_name = "users"
urlpatterns = [
    path(
        "organizations/",
        OrganizationListView.as_view(),
        name="organization-list",
    ),
    path(
        "organizations/new/",
        OrganizationCreateView.as_view(),
        name="organization-create",
    ),
    path(
        "organizations/<int:pk>/",
        OrganizationDetailView.as_view(),
        name="organization-detail",
    ),
    path(
        "organizations/<int:pk>/edit/",
        OrganizationUpdateView.as_view(),
        name="organization-update",
    ),
    path(
        "organizations/<int:pk>/delete/",
        OrganizationDeleteView.as_view(),
        name="organization-delete",
    ),
    path(
        "organizations/<int:pk>/members/<int:member_id>/roles/",
        OrganizationMemberRolesUpdateView.as_view(),
        name="organization-member-update",
    ),
    path(
        "organizations/<int:pk>/members/<int:member_id>/remove/",
        OrganizationMemberDeleteView.as_view(),
        name="organization-member-delete",
    ),
    path("~redirect/", view=user_redirect_view, name="redirect"),
    path("profile/", view=user_profile_view, name="profile"),
    path("email/", view=user_email_view, name="email"),
    path("api-key/", view=user_api_key_view, name="api-key"),
    path(
        "api-key/rotate/",
        view=user_api_key_rotate_view,
        name="api-key-rotate",
    ),
    path(
        "organizations/<int:org_id>/switch/",
        switch_current_org_view,
        name="organization-switch",
    ),
    path("<str:username>/", view=user_detail_view, name="detail"),
]
