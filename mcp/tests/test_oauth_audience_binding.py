"""
Regression tests for RFC 8707 audience binding on the OIDCProxy auth provider.

The authenticated MCP surface (``/mcp``) accepts OAuth access tokens that the
upstream Django authorization server mints. Those tokens carry an ``aud``
(audience) claim that names the specific protected resource they were issued
for. RFC 8707 (Resource Indicators) requires a resource server to *reject*
tokens whose ``aud`` does not name it — otherwise a token minted for some other
client of the same authorization server would be silently accepted here.

FastMCP's ``OIDCProxy`` only enforces the ``aud`` claim when it is constructed
with an explicit ``audience=`` argument: that value flows into the default
``JWTVerifier``, which compares it against each token's ``aud``. The server
previously built ``OIDCProxy`` *without* ``audience=``, so the verifier left the
``aud`` claim unchecked. ``Settings.effective_oauth_resource_audience`` was
computed but never wired in — a dead value masking a real authorization gap.

These tests pin the fix: ``_build_oidc_proxy`` must construct the proxy so its
token verifier enforces the effective resource audience. If someone drops the
``audience=`` argument again, ``token_verifier.audience`` reverts to ``None``
and the first test fails loudly.

The proxy must also construct its provider metadata without an HTTP request.
That property keeps an internal, disabled Cloud Run revision deployable while
the upstream Django service is deliberately offline for maintenance.
"""

from __future__ import annotations

from validibot_mcp.config import Settings
from validibot_mcp.server import _build_oidc_proxy

# Canonical audience we expect to be bound onto the JWT verifier. It must match
# what ``Settings.effective_oauth_resource_audience`` derives from the base URL
# below ("<mcp_base_url>/mcp"), since the OIDC adapter stamps the same value as
# the ``aud`` claim on issued tokens.
_EXPECTED_AUDIENCE = "https://mcp.example.test/mcp"


def _settings_with_oauth() -> Settings:
    """Build settings that enable the OIDCProxy auth path.

    A non-empty ``oauth_client_secret`` is what flips the server onto the
    OIDCProxy branch in production, so we set one here. ``mcp_base_url`` drives
    the derived resource audience.
    """

    return Settings(
        mcp_base_url="https://mcp.example.test",
        oauth_authorization_server_url="https://auth.example.test",
        oauth_client_id="validibot-mcp-server",
        oauth_client_secret="test-secret",
    )


def test_oidc_proxy_binds_resource_audience_on_verifier() -> None:
    """The built proxy's token verifier must enforce the resource audience.

    WHY: This is the security boundary itself. If the verifier's ``audience``
    is ``None``, any structurally valid token from the upstream authorization
    server is accepted regardless of which resource it was minted for (an
    RFC 8707 violation). Asserting the verifier carries the effective audience
    proves the ``aud`` claim is actually checked against this MCP surface.
    """

    settings = _settings_with_oauth()
    proxy = _build_oidc_proxy(settings)

    # The audience handed to the verifier must equal the effective resource
    # audience the OIDC adapter stamps on tokens — not None, not the bare base.
    assert settings.effective_oauth_resource_audience == _EXPECTED_AUDIENCE
    # ``_token_validator`` is the JWTVerifier OIDCProxy builds and stores;
    # ``get_token_verifier()`` is a factory that would rebuild one with default
    # args, so we assert on the configured instance.
    assert proxy._token_validator.audience == _EXPECTED_AUDIENCE


def test_custom_resource_audience_override_is_honoured() -> None:
    """An explicit ``VALIDIBOT_OAUTH_RESOURCE_AUDIENCE`` override must propagate.

    WHY: Operators can pin a non-default audience (e.g. when the MCP surface is
    fronted by a gateway whose public URL differs from ``mcp_base_url``). The
    verifier must enforce *that* value, otherwise tokens minted for the real
    audience would be rejected while the wrong audience went unchecked.
    """

    override = "https://gateway.example.test/mcp"
    settings = Settings(
        mcp_base_url="https://mcp.example.test",
        oauth_authorization_server_url="https://auth.example.test",
        oauth_client_id="validibot-mcp-server",
        oauth_client_secret="test-secret",
        oauth_resource_audience=override,
    )
    proxy = _build_oidc_proxy(settings)

    assert proxy._token_validator.audience == override


def test_oidc_proxy_uses_validibot_endpoints_without_remote_discovery() -> None:
    """Proxy construction must work while the Django issuer is unreachable.

    WHY: maintenance deployment makes Django internal-only before staging the
    MCP revision. FastMCP's normal eager discovery request would fail that
    deployment even though the MCP service is also disabled and internal.
    Building from the stable Validibot OIDC routes removes the boot-time
    network dependency without changing live OAuth forwarding.
    """

    proxy = _build_oidc_proxy(_settings_with_oauth())

    assert str(proxy.oidc_config.issuer) == "https://auth.example.test"
    assert (
        str(proxy.oidc_config.authorization_endpoint)
        == "https://auth.example.test/identity/o/authorize"
    )
    assert (
        str(proxy.oidc_config.token_endpoint) == "https://auth.example.test/identity/o/api/token"
    )
    assert (
        str(proxy.oidc_config.revocation_endpoint)
        == "https://auth.example.test/identity/o/api/revoke"
    )
    assert str(proxy.oidc_config.jwks_uri) == "https://auth.example.test/.well-known/jwks.json"


def test_oidc_endpoint_overrides_support_custom_routing() -> None:
    """Self-hosters can override each locally configured provider endpoint.

    WHY: Validibot's defaults are correct for the bundled django-allauth
    provider, but a reverse proxy may expose compatible endpoints elsewhere.
    Explicit settings keep that supported without returning to boot-time
    discovery over the network.
    """

    settings = Settings(
        mcp_base_url="https://mcp.example.test",
        oauth_authorization_server_url="https://auth.example.test",
        oauth_client_secret="test-secret",
        oauth_authorization_endpoint="https://gateway.example.test/oauth/authorize",
        oauth_token_endpoint="https://gateway.example.test/oauth/token",
        oauth_revocation_endpoint="https://gateway.example.test/oauth/revoke",
        oauth_jwks_url="https://gateway.example.test/oauth/jwks",
    )

    proxy = _build_oidc_proxy(settings)

    assert (
        str(proxy.oidc_config.authorization_endpoint)
        == "https://gateway.example.test/oauth/authorize"
    )
    assert str(proxy.oidc_config.token_endpoint) == "https://gateway.example.test/oauth/token"
    assert (
        str(proxy.oidc_config.revocation_endpoint) == "https://gateway.example.test/oauth/revoke"
    )
    assert str(proxy.oidc_config.jwks_uri) == "https://gateway.example.test/oauth/jwks"
