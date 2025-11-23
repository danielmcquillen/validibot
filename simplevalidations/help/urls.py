# help/urls.py

from django.urls import path

from . import views

app_name = "help"

urlpatterns = [
    # /help/ -> /help/index/
    path("", views.help_page, name="index"),
    # /help/<anything>/ -> /help/<anything>/
    path("<path:path>/", views.help_page, name="page"),
]
