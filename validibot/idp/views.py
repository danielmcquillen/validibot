"""OIDC metadata views for MCP-compatible authorization discovery.

These views keep the published ``/.well-known/openid-configuration`` and
``/.well-known/oauth-authorization-server`` metadata rooted at
``SITE_URL`` so MCP clients (Claude Desktop, custom agents) see a stable
issuer host even behind reverse proxies or in tests.
"""

from __future__ import annotations

from functools import lru_cache

from allauth.account.internal.decorators import login_not_required
from allauth.idp.oidc import app_settings as oidc_app_settings
from allauth.idp.oidc.adapter import get_adapter
from django.conf import settings
from django.http import JsonResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from validibot.idp.constants import CLAUDE_OIDC_GRANT_TYPES
from validibot.idp.constants import CLAUDE_OIDC_RESPONSE_TYPES
from validibot.idp.constants import CLAUDE_OIDC_SCOPES
from validibot.idp.constants import OIDC_CODE_CHALLENGE_METHODS
from validibot.idp.constants import OIDC_TOKEN_ENDPOINT_AUTH_METHODS

CANONICAL_ENDPOINTS = {
    "authorization_endpoint": "idp:oidc:authorization",
    "device_authorization_endpoint": "idp:oidc:device_code",
    "revocation_endpoint": "idp:oidc:revoke",
    "token_endpoint": "idp:oidc:token",
    "userinfo_endpoint": "idp:oidc:userinfo",
    "end_session_endpoint": "idp:oidc:logout",
    "jwks_uri": "idp:oidc:jwks",
}


@lru_cache(maxsize=1)
def _get_response_types_supported() -> tuple[str, ...]:
    """Return the public response types this cloud OIDC provider supports."""

    return tuple(CLAUDE_OIDC_RESPONSE_TYPES)


def _canonical_endpoint(name: str) -> str:
    """Build a canonical issuer-relative endpoint rooted at ``SITE_URL``."""

    return f"{settings.SITE_URL.rstrip('/')}{reverse(CANONICAL_ENDPOINTS[name])}"


def _build_openid_configuration_payload() -> dict[str, object]:
    """Return the canonicalized OIDC discovery payload for the cloud issuer."""

    userinfo_endpoint = oidc_app_settings.USERINFO_ENDPOINT
    if not userinfo_endpoint:
        userinfo_endpoint = _canonical_endpoint("userinfo_endpoint")

    return {
        "authorization_endpoint": _canonical_endpoint("authorization_endpoint"),
        "device_authorization_endpoint": _canonical_endpoint(
            "device_authorization_endpoint",
        ),
        "revocation_endpoint": _canonical_endpoint("revocation_endpoint"),
        "token_endpoint": _canonical_endpoint("token_endpoint"),
        "userinfo_endpoint": userinfo_endpoint,
        "end_session_endpoint": _canonical_endpoint("end_session_endpoint"),
        "jwks_uri": _canonical_endpoint("jwks_uri"),
        "issuer": get_adapter().get_issuer(),
        "response_types_supported": list(_get_response_types_supported()),
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }


@method_decorator(login_not_required, name="dispatch")
class OpenIDConfigurationMetadataView(View):
    """Return OIDC discovery metadata with endpoint URLs normalized to SITE_URL.

    django-allauth already exposes a discovery document, but it builds several
    endpoint URLs from the incoming request host. For Claude Desktop and MCP we
    want the published metadata to always advertise the canonical issuer host,
    even in tests or deployments behind proxies.
    """

    def get(self, request) -> JsonResponse:
        """Return an OIDC discovery document rooted at the canonical site URL."""

        normalized_response = JsonResponse(_build_openid_configuration_payload())
        normalized_response["Access-Control-Allow-Origin"] = "*"
        return normalized_response


@method_decorator(login_not_required, name="dispatch")
class OAuthAuthorizationServerMetadataView(View):
    """Expose RFC 8414 metadata derived from the allauth OIDC configuration.

    MCP clients are required to look for OAuth authorization-server metadata,
    while django-allauth natively exposes OpenID Connect discovery metadata.
    This view keeps ``/.well-known/oauth-authorization-server`` and
    ``/.well-known/openid-configuration`` aligned by deriving the RFC 8414
    document from the OIDC discovery response instead of maintaining a second
    independent metadata source.
    """

    def get(self, request) -> JsonResponse:
        """Return RFC 8414 metadata for the cloud OIDC provider."""

        oidc_config = _build_openid_configuration_payload()
        metadata = {
            "issuer": oidc_config["issuer"],
            "authorization_endpoint": oidc_config["authorization_endpoint"],
            "token_endpoint": oidc_config["token_endpoint"],
            "revocation_endpoint": oidc_config["revocation_endpoint"],
            "jwks_uri": oidc_config["jwks_uri"],
            "response_types_supported": oidc_config["response_types_supported"],
            "grant_types_supported": list(CLAUDE_OIDC_GRANT_TYPES),
            "scopes_supported": list(CLAUDE_OIDC_SCOPES),
            "token_endpoint_auth_methods_supported": list(
                OIDC_TOKEN_ENDPOINT_AUTH_METHODS,
            ),
            "code_challenge_methods_supported": list(
                OIDC_CODE_CHALLENGE_METHODS,
            ),
        }
        response = JsonResponse(metadata)
        response["Access-Control-Allow-Origin"] = "*"
        return response


oauth_authorization_server_metadata = OAuthAuthorizationServerMetadataView.as_view()
openid_configuration_metadata = OpenIDConfigurationMetadataView.as_view()
