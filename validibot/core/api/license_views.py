"""
License introspection API.

Exposes the edition and feature set of the currently running deployment so
standalone services — primarily the FastMCP server under ``mcp/`` — can
decide whether to boot without having to import Django.

The feature list is not a secret. It is derived entirely from which
commercial packages were installed at deploy time and mirrors the
public pricing page at https://validibot.com/pricing. The endpoint is
therefore unauthenticated: every caller would already learn the same
information by reading the marketing site.

Contract (see also ``mcp/src/validibot_mcp/license_check.py``):

    GET /api/v1/license/features/
    → 200 {
        "edition": "community" | "pro" | "enterprise",
        "features": ["billing", "mcp_server", ...]  # sorted
      }
"""

from drf_spectacular.utils import extend_schema
from drf_spectacular.utils import inline_serializer
from rest_framework import serializers
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from validibot.core.license import get_license


class LicenseFeaturesView(APIView):
    """
    Return the edition and feature set unlocked by the deployment's license.

    The MCP server polls this endpoint on startup (via its ASGI lifespan)
    and refuses to serve traffic unless ``mcp_server`` appears in the
    feature list. That gate is advisory — the license mechanism is the
    package-install boundary, not a cryptographic check — but it gives
    honest operators a clear signal when a commercial feature is being
    run without the matching commercial package installed.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Get the deployment's edition and feature set",
        description=(
            "Returns the Validibot edition and the commercial features "
            "unlocked by the currently installed license. Used by the "
            "standalone MCP server at startup to gate on the ``mcp_server`` "
            "feature, and safe to call anonymously — the feature list "
            "mirrors the public pricing page."
        ),
        responses={
            200: inline_serializer(
                name="LicenseFeaturesResponse",
                fields={
                    "edition": serializers.ChoiceField(
                        choices=["community", "pro", "enterprise"],
                        help_text="The Validibot edition of this deployment.",
                    ),
                    "features": serializers.ListField(
                        child=serializers.CharField(),
                        help_text=(
                            "Sorted list of commercial feature identifiers "
                            "this license unlocks (matches "
                            "``CommercialFeature`` values)."
                        ),
                    ),
                },
            ),
        },
        tags=["License"],
    )
    def get(self, request):
        """Return the deployment's license edition and feature set."""
        lic = get_license()
        return Response(
            {
                "edition": lic.edition.value,
                "features": sorted(lic.features),
            },
            status=status.HTTP_200_OK,
        )
