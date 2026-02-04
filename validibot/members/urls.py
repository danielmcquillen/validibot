from django.urls import path

from validibot.members import views

app_name = "members"

urlpatterns = [
    path(
        "",
        views.MemberListView.as_view(),
        name="member_list",
    ),
    path(
        "invites/",
        views.InviteFormView.as_view(),
        name="invite_form",
    ),
    path(
        "invites/search/",
        views.InviteSearchView.as_view(),
        name="invite_search",
    ),
    path(
        "invites/create/",
        views.InviteCreateView.as_view(),
        name="invite_create",
    ),
    path(
        "invites/<uuid:invite_id>/cancel/",
        views.InviteCancelView.as_view(),
        name="invite_cancel",
    ),
    path(
        "<int:member_id>/edit/",
        views.MemberUpdateView.as_view(),
        name="member_edit",
    ),
    path(
        "<int:member_id>/delete/",
        views.MemberDeleteView.as_view(),
        name="member_delete",
    ),
    path(
        "<int:member_id>/delete/confirm/",
        views.MemberDeleteConfirmView.as_view(),
        name="member_delete_confirm",
    ),
    # Guest management
    path(
        "guests/",
        views.GuestListView.as_view(),
        name="guest_list",
    ),
    path(
        "guests/invite/",
        views.GuestInviteCreateView.as_view(),
        name="guest_invite_create",
    ),
    path(
        "guests/invites/<uuid:invite_id>/cancel/",
        views.GuestInviteCancelView.as_view(),
        name="guest_invite_cancel",
    ),
    path(
        "guests/<int:user_id>/revoke-all/",
        views.GuestRevokeAllView.as_view(),
        name="guest_revoke_all",
    ),
]
