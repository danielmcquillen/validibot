from django.urls import path

from simplevalidations.projects import views

app_name = "projects"

urlpatterns = [
    path("", views.ProjectListView.as_view(), name="project-list"),
    path("new/", views.ProjectCreateView.as_view(), name="project-create"),
    path("<int:pk>/edit/", views.ProjectUpdateView.as_view(), name="project-update"),
    path("<int:pk>/delete/", views.ProjectDeleteView.as_view(), name="project-delete"),
]
