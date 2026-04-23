"""OIDC adapter customizations for the Validibot authorization server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from allauth.idp.oidc.adapter import DefaultOIDCAdapter
from django.conf import settings
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from collections.abc import Iterable


class ValidibotOIDCAdapter(DefaultOIDCAdapter):
    """Customize issuer, scope labels, and MCP audience claims for Validibot.

    This adapter sits at the boundary between django-allauth's generic OIDC
    provider and Validibot's MCP-specific needs. The Validibot Django app
    remains the authorization server, but the emitted metadata and JWT
    access tokens need a stable issuer based on the app hostname plus an
    audience claim for the MCP resource.

    Lives in the community repo so self-hosted Pro deployments can issue
    tokens that their MCP server will accept. Cloud overrides the MCP
    audience value via its own settings.
    """

    scope_display = {
        **DefaultOIDCAdapter.scope_display,
        "validibot:mcp": _("Use Validibot workflows through the MCP server"),
    }

    def get_issuer(self) -> str:
        """Return the canonical issuer URL for this deployment.

        Using ``SITE_URL`` keeps the issuer stable behind reverse proxies
        and in tests where the request host is ``testserver`` instead of
        the public hostname.
        """

        site_url = getattr(settings, "SITE_URL", "").rstrip("/")
        if site_url:
            return site_url
        return super().get_issuer()

    def populate_access_token(
        self,
        access_token: dict,
        *,
        client,
        scopes: Iterable[str],
        user,
        **kwargs,
    ) -> None:
        """Add MCP-specific claims to JWT access tokens when appropriate.

        The MCP audience claim is only added when the token includes the
        ``validibot:mcp`` scope. This keeps the adapter compatible with
        future non-MCP clients that may share the same allauth issuer.
        """

        super().populate_access_token(
            access_token,
            client=client,
            scopes=scopes,
            user=user,
            **kwargs,
        )
        if "validibot:mcp" not in scopes:
            return

        audience = getattr(settings, "IDP_OIDC_MCP_RESOURCE_AUDIENCE", "").rstrip("/")
        if audience:
            access_token["aud"] = audience
