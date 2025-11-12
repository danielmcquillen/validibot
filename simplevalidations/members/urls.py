from django.urls import path

from simplevalidations.members import views

app_name = "members"

urlpatterns = [
    path("", views.MemberListView.as_view(), name="member_list"),
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
]
