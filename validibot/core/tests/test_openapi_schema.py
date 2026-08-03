"""Regression tests for Validibot's generated OpenAPI contract.

The runtime API supports both browser sessions and custom Bearer API keys,
while workflow and validation-run serializers expose the same output-retention
choices under different field names. These tests protect the explicit schema
customizations that keep those runtime contracts accurate in Swagger, ReDoc,
and generated clients. Without them, ``manage.py check --deploy`` reports
``drf_spectacular.W001`` and the generated schema is incomplete or needlessly
duplicates an enum component.
"""

from drf_spectacular.generators import SchemaGenerator

from validibot.submissions.constants import OutputRetention


def _generate_schema() -> dict:
    """Generate the public API schema through the production code path."""

    return SchemaGenerator().get_schema(request=None, public=True)


def test_schema_documents_custom_bearer_authentication():
    """API-key clients need a Bearer scheme in the published API contract.

    The authenticator is a custom DRF class, so drf-spectacular cannot infer
    this definition. Pinning both the scheme and its use on an operation keeps
    generated clients from offering cookie authentication as the only option.
    """

    schema = _generate_schema()

    assert schema["components"]["securitySchemes"]["bearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert any(
        requirement == {"bearerAuth": []}
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict)
        for requirement in operation.get("security", [])
    )


def test_schema_uses_one_output_retention_enum_component():
    """One domain choice set should produce one stable client-side enum.

    ``output_retention`` and ``output_retention_policy`` deliberately share
    ``OutputRetention``. If schema naming falls back to field names, client
    generators see duplicate types and deployment checks warn about ambiguity.
    """

    schema = _generate_schema()
    expected_values = {choice.value for choice in OutputRetention}
    matching_components = [
        name
        for name, component in schema["components"]["schemas"].items()
        if set(component.get("enum", [])) == expected_values
    ]

    assert matching_components == ["OutputRetentionEnum"]
