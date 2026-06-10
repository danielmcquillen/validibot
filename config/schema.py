"""OpenAPI schema customization hooks for drf-spectacular.

Why this exists: the interactive API reference at ``/api/v1/docs/`` (Swagger
UI) and ``/api/v1/redoc/`` is generated from every route DRF can see. That
includes ``/api/v1/mcp/*`` — the MCP helper surface, which is
service-to-service only (authenticated with ``X-MCP-Service-Key`` or a Cloud
Run OIDC identity token, never a user API key). Showing those routes in the
user-facing reference invites users to call endpoints that will always 403
for them, and the plain ``APIView`` implementations have no serializer for
spectacular to introspect (each one logged a generation error).

Excluding them here keeps the published reference scoped to the API users
can actually call. The MCP helper surface is documented for integrators in
``docs/dev_docs/mcp/index.md`` instead.
"""

from __future__ import annotations

# Path prefixes that are internal/service-to-service and must not appear in
# the public OpenAPI schema.
INTERNAL_PATH_PREFIXES = ("/api/v1/mcp/",)


def exclude_internal_paths(endpoints, **kwargs):
    """drf-spectacular PREPROCESSING_HOOK that drops internal routes.

    Args:
        endpoints: list of (path, path_regex, method, callback) tuples that
            spectacular collected from the URL conf.

    Returns:
        The same list minus any endpoint whose path starts with one of
        ``INTERNAL_PATH_PREFIXES``.
    """
    return [
        (path, path_regex, method, callback)
        for path, path_regex, method, callback in endpoints
        if not path.startswith(INTERNAL_PATH_PREFIXES)
    ]
