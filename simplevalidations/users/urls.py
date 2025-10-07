from django.urls import path

from simplevalidations.users.views import (
    user_api_key_rotate_view,
    user_api_key_view,
    user_detail_view,
    user_email_view,
    user_profile_view,
    user_redirect_view,
)

app_name = "users"
urlpatterns = [
    path("~redirect/", view=user_redirect_view, name="redirect"),
    path("profile/", view=user_profile_view, name="profile"),
    path("email/", view=user_email_view, name="email"),
    path("api-key/", view=user_api_key_view, name="api-key"),
    path("api-key/rotate/", view=user_api_key_rotate_view, name="api-key-rotate"),
    path("<str:username>/", view=user_detail_view, name="detail"),
]
