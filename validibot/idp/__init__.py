"""OIDC provider customizations for Validibot.

This app sits on top of django-allauth's generic OIDC authorization-server
implementation and adds the Validibot-specific pieces needed to issue
JWT access tokens that the standalone FastMCP server (and Claude Desktop
via OAuth) will accept:

- A custom adapter (``adapter.ValidibotOIDCAdapter``) that stamps the
  ``validibot:mcp`` scope label and an audience claim onto access tokens.
- RFC 8414 / OpenID Connect discovery views rooted at ``SITE_URL`` so the
  published metadata is stable behind proxies and in tests.
- An ``ensure_oidc_clients`` management command that idempotently creates
  the two OIDC clients used by the MCP OAuth flow (Claude Desktop public
  client, MCP server confidential client).

Placement note: this app lives in the community repo (not in
``validibot-pro`` or ``validibot-cloud``) because self-hosted Pro users
need MCP OAuth to work. There is no proprietary IP in the code — it's a
thin customization of a public spec (OIDC) wrapped around django-allauth.
Cloud overrides the MCP audience URL and supplies the confidential
client's secret via its own settings, but the business logic is here.
"""
