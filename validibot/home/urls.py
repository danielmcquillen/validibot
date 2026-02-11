"""URL configuration for the home app."""

from django.urls import path

from validibot.home import views

app_name = "home"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
]
