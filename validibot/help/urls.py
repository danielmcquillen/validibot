# help/urls.py

from django.urls import path

from . import views

app_name = "help"

urlpatterns = [
    path("", views.help_page, name="help_index"),
    path("<path:path>/", views.help_page, name="help_page"),
]
