"""The worker URLConf must not expose interactive API docs.

ADR 04-23 §hyg.worker_docs_exposed: the worker role is private and IAM-gated
and should serve only health checks and internal APIs. Swagger/ReDoc/schema
endpoints would leak the internal API surface to anyone reaching the worker
origin, so they belong only on the web role.
"""

from __future__ import annotations

from config import urls_worker


def test_worker_urlconf_excludes_api_docs():
    """The worker urlpatterns expose health + internal API, but no API docs.

    Asserting on the route *names* keeps this robust to path changes: the
    Swagger (``api-docs``), ReDoc (``api-redoc``), and schema (``api-schema``)
    routes must be absent, while the health check stays present.
    """
    names = {getattr(pattern, "name", None) for pattern in urls_worker.urlpatterns}

    assert "api-docs" not in names
    assert "api-redoc" not in names
    assert "api-schema" not in names
    assert "health-check" in names
