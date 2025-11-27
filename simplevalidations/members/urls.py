from django.urls import path

from simplevalidations.members import views

app_name = "members"

urlpatterns = [
    path("", views.MemberListView.as_view(), name="member_list"),
    path("invites/search/", views.InviteSearchView.as_view(), name="invite_search"),
    path("invites/create/", views.InviteCreateView.as_view(), name="invite_create"),
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
]
