"""Minimal URLConf for Validibot OIDC provider tests.

These tests only need the OIDC issuer endpoints, not the entire URL stack
with all the API overrides. Keeping a focused test URLConf avoids
unrelated optional dependencies.
"""

from django.urls import include
from django.urls import path

from validibot.idp.views import oauth_authorization_server_metadata
from validibot.idp.views import openid_configuration_metadata

urlpatterns = [
    path(
        ".well-known/openid-configuration",
        openid_configuration_metadata,
        name="openid-configuration-metadata",
    ),
    path(
        ".well-known/oauth-authorization-server",
        oauth_authorization_server_metadata,
        name="oauth-authorization-server-metadata",
    ),
    path(
        "",
        include("allauth.idp.urls", namespace="idp"),
    ),
]
