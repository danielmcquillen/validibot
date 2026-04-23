"""Ensure cloud-managed OIDC clients exist with the expected configuration.

This command manages two OIDC clients needed for the MCP OAuth flow:

1. **Claude Desktop public client** — used by Claude Desktop / Claude Code
   to authenticate end users via the standard OAuth 2.1 PKCE flow.

2. **MCP server confidential client** — used by the MCP Cloud Run service
   to proxy OAuth flows on behalf of MCP clients that can't follow external
   authorization endpoints (Claude Desktop's known limitation, see
   https://github.com/anthropics/claude-ai-mcp/issues/82).

Both clients are intentionally idempotent: the command creates them if missing,
updates them if configuration has drifted, and is safe to run on every deploy
after migrations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from allauth.idp.oidc.models import Client as OIDCClient
from django.conf import settings
from django.core.management.base import BaseCommand


class ClientType(Enum):
    """OIDC client type — maps to allauth's Client.Type choices."""

    PUBLIC = "public"
    CONFIDENTIAL = "confidential"


@dataclass(frozen=True)
class ManagedOIDCClient:
    """Describe one OIDC client registration managed by deployment automation.

    The command compares the live Django row against this dataclass and only
    writes changes when drift is detected. That keeps the command safe to run
    on every deploy while still correcting configuration changes such as new
    redirect URIs or consent behavior.
    """

    client_id: str
    name: str
    client_type: ClientType
    redirect_uris: tuple[str, ...]
    scopes: tuple[str, ...]
    default_scopes: tuple[str, ...]
    grant_types: tuple[str, ...]
    response_types: tuple[str, ...]
    skip_consent: bool
    secret: str = ""


class Command(BaseCommand):
    """Create or update the cloud OIDC clients needed for MCP OAuth."""

    help = "Create or update the cloud-managed OIDC clients for the MCP OAuth flow."

    def handle(self, *args, **options) -> None:
        for definition in self._get_managed_clients():
            self._upsert_client(definition)

    def _get_managed_clients(self) -> list[ManagedOIDCClient]:
        """Return all managed client definitions from Django settings."""

        clients = [
            # Public client for Claude Desktop / Claude Code end-user auth.
            ManagedOIDCClient(
                client_id=settings.IDP_OIDC_CLAUDE_CLIENT_ID,
                name=settings.IDP_OIDC_CLAUDE_CLIENT_NAME,
                client_type=ClientType.PUBLIC,
                redirect_uris=tuple(settings.IDP_OIDC_CLAUDE_REDIRECT_URIS),
                scopes=tuple(settings.IDP_OIDC_CLAUDE_SCOPES),
                default_scopes=tuple(settings.IDP_OIDC_CLAUDE_SCOPES),
                grant_types=tuple(settings.IDP_OIDC_CLAUDE_GRANT_TYPES),
                response_types=tuple(settings.IDP_OIDC_CLAUDE_RESPONSE_TYPES),
                skip_consent=settings.IDP_OIDC_CLAUDE_SKIP_CONSENT,
            ),
        ]

        # Confidential client for the MCP server's OIDCProxy.
        # Only registered when a client secret is configured — without a
        # secret, the OIDCProxy is disabled and only legacy API tokens work.
        mcp_secret = getattr(
            settings,
            "IDP_OIDC_MCP_SERVER_CLIENT_SECRET",
            "",
        )
        if mcp_secret:
            clients.append(
                ManagedOIDCClient(
                    client_id=settings.IDP_OIDC_MCP_SERVER_CLIENT_ID,
                    name=settings.IDP_OIDC_MCP_SERVER_CLIENT_NAME,
                    client_type=ClientType.CONFIDENTIAL,
                    redirect_uris=tuple(
                        settings.IDP_OIDC_MCP_SERVER_REDIRECT_URIS,
                    ),
                    scopes=tuple(settings.IDP_OIDC_MCP_SERVER_SCOPES),
                    default_scopes=tuple(settings.IDP_OIDC_MCP_SERVER_SCOPES),
                    grant_types=tuple(settings.IDP_OIDC_MCP_SERVER_GRANT_TYPES),
                    response_types=tuple(
                        settings.IDP_OIDC_MCP_SERVER_RESPONSE_TYPES,
                    ),
                    skip_consent=True,
                    secret=mcp_secret,
                ),
            )

        return clients

    def _upsert_client(self, definition: ManagedOIDCClient) -> None:
        """Create or reconcile a single managed OIDC client row."""

        allauth_type = (
            OIDCClient.Type.PUBLIC
            if definition.client_type == ClientType.PUBLIC
            else OIDCClient.Type.CONFIDENTIAL
        )

        client, created = OIDCClient.objects.get_or_create(
            id=definition.client_id,
            defaults={
                "name": definition.name,
                "type": allauth_type,
                "skip_consent": definition.skip_consent,
            },
        )

        changed = created
        changed |= self._set_attr(client, "name", definition.name)
        changed |= self._set_attr(client, "type", allauth_type)
        changed |= self._set_attr(client, "skip_consent", definition.skip_consent)

        # Set the client secret for confidential clients.  The secret
        # is stored hashed, so we use check_secret() for drift detection
        # and set_secret() to hash before saving.
        if definition.secret and not client.check_secret(definition.secret):
            client.set_secret(definition.secret)
            changed = True

        changed |= self._set_text_values(
            client.get_redirect_uris,
            client.set_redirect_uris,
            definition.redirect_uris,
        )
        changed |= self._set_text_values(
            client.get_scopes,
            client.set_scopes,
            definition.scopes,
        )
        changed |= self._set_text_values(
            client.get_default_scopes,
            client.set_default_scopes,
            definition.default_scopes,
        )
        changed |= self._set_text_values(
            client.get_grant_types,
            client.set_grant_types,
            definition.grant_types,
        )
        changed |= self._set_text_values(
            client.get_response_types,
            client.set_response_types,
            definition.response_types,
        )

        if changed:
            client.full_clean()
            client.save()

        client_id = definition.client_id
        client_type = definition.client_type.value
        if created:
            msg = f"Created OIDC client '{client_id}' ({client_type})."
        elif changed:
            msg = f"Updated OIDC client '{client_id}' ({client_type})."
        else:
            msg = f"OIDC client '{client_id}' is already up to date."
        self.stdout.write(self.style.SUCCESS(msg))

    @staticmethod
    def _set_attr(client: OIDCClient, field_name: str, desired_value: object) -> bool:
        """Update a simple model field when the current value has drifted."""

        if getattr(client, field_name) == desired_value:
            return False
        setattr(client, field_name, desired_value)
        return True

    @staticmethod
    def _set_text_values(
        getter,
        setter,
        desired_values: tuple[str, ...],
    ) -> bool:
        """Update newline-backed OIDC fields through the model helper methods."""

        if tuple(getter()) == desired_values:
            return False
        setter(list(desired_values))
        return True
