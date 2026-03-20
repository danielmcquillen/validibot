"""Authoring-time parsing and validation for workflow input contracts.

This module converts author-provided text — either raw JSON Schema or a
restricted subset of Pydantic 2 class syntax — into the canonical JSON Schema
stored on ``Workflow.input_schema``.

Security posture
~~~~~~~~~~~~~~~~

**Pydantic mode never executes pasted Python.** It parses the text with
``ast.parse()`` and walks the AST against a strict allowlist of node shapes.
If any disallowed construct appears, the entire input is rejected — we do not
attempt to strip or sanitize partial input.

Resource limits (character count, line count, AST node count) are enforced
to prevent parser-based denial-of-service.
"""

from __future__ import annotations

import ast
import json
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

# ── Resource limits ──────────────────────────────────────────────────────

MAX_SCHEMA_CHARS = 20_000
MAX_SCHEMA_LINES = 500
MAX_AST_NODES = 500

# ── Supported v1 schema subset ──────────────────────────────────────────

SUPPORTED_TYPES = {"string", "integer", "number", "boolean"}

SUPPORTED_PROPERTY_KEYS = {
    "type",
    "description",
    "default",
    "enum",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "units",
    # Pydantic-style keys (also accepted for round-trip fidelity)
    "ge",
    "gt",
    "le",
    "lt",
    "title",
}

# ── JSON Schema authoring ───────────────────────────────────────────────


def parse_json_schema_input(text: str) -> dict:
    """Parse and validate author-provided JSON Schema text.

    Returns the normalized canonical JSON Schema dict on success.
    Raises ``ValidationError`` with a user-facing message on failure.
    """
    _enforce_resource_limits(text)

    try:
        schema = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            _("Invalid JSON: %(error)s (line %(line)s, column %(col)s)"),
            params={"error": str(exc.msg), "line": exc.lineno, "col": exc.colno},
            code="invalid_json",
        ) from exc

    if not isinstance(schema, dict):
        raise ValidationError(
            _("Input schema must be a JSON object."),
            code="not_object",
        )

    validate_schema_subset(schema)
    return schema


# ── Pydantic authoring ──────────────────────────────────────────────────

# Allowed type annotation names in Pydantic mode.
_ALLOWED_TYPE_NAMES = {"str", "int", "float", "bool"}

# Allowed Field() keyword arguments.
_ALLOWED_FIELD_KWARGS = {
    "description",
    "default",
    "ge",
    "gt",
    "le",
    "lt",
    "json_schema_extra",
    "title",
}

# Mapping from Pydantic type name to JSON Schema type.
_PYDANTIC_TYPE_MAP = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
}


def parse_pydantic_input(text: str) -> dict:
    """Parse a restricted Pydantic 2 BaseModel class and convert to JSON Schema.

    The accepted subset:
    - A single ``BaseModel`` subclass
    - Flat fields only (no nested models)
    - Types: ``str``, ``int``, ``float``, ``bool``, ``Optional[...]``, ``Literal[...]``
    - ``Field(...)`` metadata for ``description``, ``default``, ``ge``, ``gt``,
      ``le``, ``lt``, and ``json_schema_extra={"units": ...}``

    Raises ``ValidationError`` with user-facing messages on failure.
    """
    _enforce_resource_limits(text)

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise ValidationError(
            _("Python syntax error: %(error)s (line %(line)s)"),
            params={"error": exc.msg, "line": exc.lineno or "?"},
            code="syntax_error",
        ) from exc

    # Enforce AST node count limit
    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > MAX_AST_NODES:
        raise ValidationError(
            _("Input too complex: %(count)s AST nodes (limit %(limit)s)."),
            params={"count": node_count, "limit": MAX_AST_NODES},
            code="too_complex",
        )

    # Find exactly one class that inherits from BaseModel
    classes = [
        node for node in ast.iter_child_nodes(tree) if isinstance(node, ast.ClassDef)
    ]

    # Allow import statements at the top level (they're harmless — we never
    # execute the code) but reject anything else that isn't the class.
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.ClassDef, ast.Import, ast.ImportFrom)):
            continue
        raise ValidationError(
            _(
                "Only a single BaseModel class and import statements are allowed "
                "(found %(node)s on line %(line)s)."
            ),
            params={"node": type(node).__name__, "line": getattr(node, "lineno", "?")},
            code="disallowed_top_level",
        )

    if len(classes) != 1:
        raise ValidationError(
            _("Expected exactly one class definition, found %(count)s."),
            params={"count": len(classes)},
            code="wrong_class_count",
        )

    cls = classes[0]

    # Validate base classes — must include BaseModel
    base_names = [_get_name(b) for b in cls.bases]
    if "BaseModel" not in base_names:
        raise ValidationError(
            _("The class must inherit from BaseModel."),
            code="not_basemodel",
        )

    # Walk class body — only allow annotated assignments
    properties: dict[str, dict] = {}
    required: list[str] = []
    title = cls.name

    for stmt in cls.body:
        # Allow Pass (empty class body), Expr(Constant(str)) (docstrings)
        if isinstance(stmt, ast.Pass):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue

        if not isinstance(stmt, ast.AnnAssign):
            raise ValidationError(
                _(
                    "Unsupported statement on line %(line)s: only annotated field "
                    "assignments are allowed (e.g. ``name: str``). "
                    "Methods, validators, and other constructs are not supported."
                ),
                params={"line": stmt.lineno},
                code="disallowed_statement",
            )

        if not isinstance(stmt.target, ast.Name):
            raise ValidationError(
                _("Unsupported assignment target on line %(line)s."),
                params={"line": stmt.lineno},
                code="bad_target",
            )

        field_name = stmt.target.id
        annotation = stmt.annotation
        default_node = stmt.value  # may be None (no default)

        # Parse the type annotation
        type_info = _parse_annotation(annotation, stmt.lineno)

        # Build the property dict
        prop: dict[str, Any] = {"type": type_info["json_type"]}

        if type_info.get("enum_values"):
            prop["enum"] = type_info["enum_values"]

        is_optional = type_info.get("optional", False)

        # Parse Field(...) or literal default
        if default_node is not None:
            field_meta = _parse_default_or_field(default_node, stmt.lineno)
            prop.update(field_meta.get("schema_props", {}))
            if "default" in field_meta:
                prop["default"] = field_meta["default"]
                is_optional = True
        elif is_optional:
            # Optional with no default — default is None, omit from schema
            pass

        if not is_optional and "default" not in prop:
            required.append(field_name)

        properties[field_name] = prop

    schema: dict[str, Any] = {
        "title": title,
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required

    validate_schema_subset(schema)
    return schema


# ── Schema subset validation ────────────────────────────────────────────


def validate_schema_subset(schema: dict) -> None:
    """Validate that a schema conforms to the supported v1 subset.

    Raises ``ValidationError`` if the schema uses unsupported constructs.
    """
    if schema.get("type") != "object":
        raise ValidationError(
            _('Input schema must have "type": "object".'),
            code="not_object_type",
        )

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValidationError(
            _('Input schema must have a "properties" object.'),
            code="missing_properties",
        )

    required_fields = schema.get("required", [])
    if not isinstance(required_fields, list):
        raise ValidationError(
            _('"required" must be a list of field names.'),
            code="bad_required",
        )

    for name, prop in properties.items():
        if not isinstance(prop, dict):
            raise ValidationError(
                _('Property "%(name)s" must be an object.'),
                params={"name": name},
                code="bad_property",
            )

        json_type = prop.get("type", "string")
        if json_type not in SUPPORTED_TYPES:
            raise ValidationError(
                _(
                    'Property "%(name)s" has unsupported type "%(type)s". '
                    "Supported types: %(supported)s."
                ),
                params={
                    "name": name,
                    "type": json_type,
                    "supported": ", ".join(sorted(SUPPORTED_TYPES)),
                },
                code="unsupported_type",
            )

        # Reject non-required booleans without explicit default — checkbox
        # UX collapses "absent" and false, which is lossy.
        if json_type == "boolean" and name not in required_fields:
            if "default" not in prop:
                raise ValidationError(
                    _(
                        'Optional boolean field "%(name)s" is not supported. '
                        "Boolean fields must be required, or have an explicit default."
                    ),
                    params={"name": name},
                    code="optional_boolean",
                )

    # Reject unsupported property keywords — the runtime adapters
    # (Pydantic model builder, Django form builder) only honour keys in
    # SUPPORTED_PROPERTY_KEYS.  Silently accepting extra keywords would
    # weaken the contract: the schema would promise constraints the
    # runtime cannot enforce.
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        unsupported_keys = set(prop.keys()) - SUPPORTED_PROPERTY_KEYS
        if unsupported_keys:
            raise ValidationError(
                _(
                    'Property "%(name)s" uses unsupported keywords: '
                    "%(keys)s. Supported: %(supported)s."
                ),
                params={
                    "name": name,
                    "keys": ", ".join(sorted(unsupported_keys)),
                    "supported": ", ".join(sorted(SUPPORTED_PROPERTY_KEYS)),
                },
                code="unsupported_property_keys",
            )

    # Reject nested objects and arrays
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        prop_type = prop.get("type")
        if prop_type in ("object", "array"):
            raise ValidationError(
                _(
                    "Nested objects and arrays are not supported in v1 "
                    '(found on property "%(name)s").'
                ),
                params={"name": name},
                code="nested_not_supported",
            )
        # Also reject $ref, allOf, oneOf, anyOf
        for schema_keyword in ("$ref", "allOf", "oneOf", "anyOf"):
            if schema_keyword in prop:
                raise ValidationError(
                    _(
                        'Schema composition keyword "%(keyword)s" is not supported '
                        'in v1 (found on property "%(name)s").'
                    ),
                    params={"keyword": schema_keyword, "name": name},
                    code="composition_not_supported",
                )


# ── Internal helpers ─────────────────────────────────────────────────────


def _enforce_resource_limits(text: str) -> None:
    """Reject oversized input before parsing."""
    if len(text) > MAX_SCHEMA_CHARS:
        raise ValidationError(
            _("Input too large: %(chars)s characters (limit %(limit)s)."),
            params={"chars": len(text), "limit": MAX_SCHEMA_CHARS},
            code="too_large",
        )
    if text.count("\n") + 1 > MAX_SCHEMA_LINES:
        raise ValidationError(
            _("Input too many lines: %(lines)s (limit %(limit)s)."),
            params={"lines": text.count("\n") + 1, "limit": MAX_SCHEMA_LINES},
            code="too_many_lines",
        )


def _get_name(node: ast.expr) -> str:
    """Extract a simple name string from an AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _parse_annotation(node: ast.expr, lineno: int) -> dict:
    """Parse a type annotation AST node into type info.

    Returns a dict with:
    - ``json_type``: the JSON Schema type string
    - ``optional``: bool, whether the type is Optional
    - ``enum_values``: list of literal values for Literal types, or None
    """
    # Simple name: str, int, float, bool
    if isinstance(node, ast.Name):
        if node.id not in _ALLOWED_TYPE_NAMES:
            raise ValidationError(
                _(
                    'Unsupported type "%(type)s" on line %(line)s. '
                    "Supported: str, int, float, bool, Optional[...], Literal[...]."
                ),
                params={"type": node.id, "line": lineno},
                code="unsupported_annotation_type",
            )
        return {"json_type": _PYDANTIC_TYPE_MAP[node.id]}

    # Subscript: Optional[X] or Literal[values]
    if isinstance(node, ast.Subscript):
        outer = _get_name(node.value)

        if outer == "Optional":
            inner = _parse_annotation(node.slice, lineno)
            inner["optional"] = True
            return inner

        if outer == "Literal":
            values = _extract_literal_values(node.slice, lineno)
            # Infer JSON type from the literal values
            if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
                json_type = "integer"
            elif all(isinstance(v, (int, float)) for v in values):
                json_type = "number"
            elif all(isinstance(v, str) for v in values):
                json_type = "string"
            else:
                raise ValidationError(
                    _(
                        "Literal values on line %(line)s must all be the same type "
                        "(int, float, or str)."
                    ),
                    params={"line": lineno},
                    code="mixed_literal_types",
                )
            return {"json_type": json_type, "enum_values": values}

        raise ValidationError(
            _('Unsupported generic type "%(type)s" on line %(line)s.'),
            params={"type": outer, "line": lineno},
            code="unsupported_generic",
        )

    # ast.Constant for string annotations (not typical but handle gracefully)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        raise ValidationError(
            _(
                "String annotations are not supported on line %(line)s. "
                "Use direct type names (e.g. ``int`` not ``'int'``)."
            ),
            params={"line": lineno},
            code="string_annotation",
        )

    raise ValidationError(
        _("Unsupported annotation on line %(line)s."),
        params={"line": lineno},
        code="unsupported_annotation",
    )


def _extract_literal_values(node: ast.expr, lineno: int) -> list:
    """Extract literal values from a Literal[...] subscript."""
    if isinstance(node, ast.Tuple):
        return [_extract_single_literal(elt, lineno) for elt in node.elts]
    return [_extract_single_literal(node, lineno)]


def _extract_single_literal(node: ast.expr, lineno: int) -> Any:
    """Extract a single literal value from an AST node."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)):
            return node.value
    # Negative numbers: ast.UnaryOp(op=USub, operand=Constant)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        return -node.operand.value

    raise ValidationError(
        _(
            "Unsupported literal value on line %(line)s. "
            "Only string, integer, float, and boolean literals are allowed."
        ),
        params={"line": lineno},
        code="unsupported_literal",
    )


def _parse_default_or_field(node: ast.expr, lineno: int) -> dict:
    """Parse a default value or Field(...) call from an assignment RHS.

    Returns a dict with optional keys:
    - ``default``: the default value
    - ``schema_props``: dict of JSON Schema property keys to add
    """
    # Literal default value
    if isinstance(node, ast.Constant):
        return {"default": node.value}

    # Negative number default
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        return {"default": -node.operand.value}

    # Field(...) call
    if isinstance(node, ast.Call):
        func_name = _get_name(node.func)
        if func_name != "Field":
            raise ValidationError(
                _(
                    'Unsupported call "%(func)s(...)" on line %(line)s. '
                    "Only ``Field(...)`` is allowed."
                ),
                params={"func": func_name, "line": lineno},
                code="disallowed_call",
            )
        return _parse_field_call(node, lineno)

    raise ValidationError(
        _(
            "Unsupported default expression on line %(line)s. "
            "Only literal values and ``Field(...)`` are allowed."
        ),
        params={"line": lineno},
        code="unsupported_default",
    )


def _parse_field_call(node: ast.Call, lineno: int) -> dict:
    """Parse a ``Field(...)`` call and extract supported kwargs."""
    result: dict = {"schema_props": {}}

    # Reject positional args except a single literal default
    if node.args:
        if len(node.args) == 1:
            val = _safe_literal_extract(node.args[0], lineno)
            if val is not None or (
                isinstance(node.args[0], ast.Constant) and node.args[0].value is None
            ):
                result["default"] = val
        else:
            raise ValidationError(
                _(
                    "Field() on line %(line)s: only one positional argument "
                    "(default value) is allowed."
                ),
                params={"line": lineno},
                code="too_many_positional",
            )

    for kw in node.keywords:
        if kw.arg is None:
            raise ValidationError(
                _("Field() on line %(line)s: **kwargs are not allowed."),
                params={"line": lineno},
                code="kwargs_not_allowed",
            )

        if kw.arg == "default_factory":
            raise ValidationError(
                _(
                    "Field(default_factory=...) on line %(line)s is not supported. "
                    "Use a literal default value."
                ),
                params={"line": lineno},
                code="default_factory",
            )

        if kw.arg not in _ALLOWED_FIELD_KWARGS:
            raise ValidationError(
                _('Field() keyword "%(kwarg)s" on line %(line)s is not supported.'),
                params={"kwarg": kw.arg, "line": lineno},
                code="unsupported_field_kwarg",
            )

        val = _safe_literal_extract(kw.value, lineno)

        if kw.arg == "default":
            result["default"] = val
        elif kw.arg == "description":
            if not isinstance(val, str):
                raise ValidationError(
                    _("Field(description=...) on line %(line)s must be a string."),
                    params={"line": lineno},
                    code="description_not_str",
                )
            result["schema_props"]["description"] = val
        elif kw.arg == "json_schema_extra":
            if not isinstance(val, dict):
                raise ValidationError(
                    _("Field(json_schema_extra=...) on line %(line)s must be a dict."),
                    params={"line": lineno},
                    code="extra_not_dict",
                )
            # Flatten onto schema props (e.g. {"units": "m²K/W"})
            for extra_key, extra_val in val.items():
                result["schema_props"][extra_key] = extra_val
        elif kw.arg in ("ge", "gt", "le", "lt"):
            # Numeric constraints — map to JSON Schema keywords
            json_key = {
                "ge": "minimum",
                "gt": "exclusiveMinimum",
                "le": "maximum",
                "lt": "exclusiveMaximum",
            }[kw.arg]
            result["schema_props"][json_key] = val
        elif kw.arg == "title":
            result["schema_props"]["title"] = val

    return result


def _safe_literal_extract(node: ast.expr, lineno: int) -> Any:
    """Extract a literal value from an AST node without executing code.

    Only allows: strings, numbers, booleans, None, lists, tuples, dicts
    of literals.  Rejects any callable, attribute, or complex expression.
    """
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        if isinstance(node.operand, ast.Constant) and isinstance(
            node.operand.value, (int, float)
        ):
            return -node.operand.value

    if isinstance(node, (ast.List, ast.Tuple)):
        return [_safe_literal_extract(elt, lineno) for elt in node.elts]

    if isinstance(node, ast.Dict):
        keys = [_safe_literal_extract(k, lineno) for k in node.keys if k is not None]
        values = [_safe_literal_extract(v, lineno) for v in node.values]
        if len(keys) != len(node.keys):
            raise ValidationError(
                _("Dict unpacking (**) on line %(line)s is not allowed."),
                params={"line": lineno},
                code="dict_unpacking",
            )
        return dict(zip(keys, values, strict=True))

    raise ValidationError(
        _(
            "Unsupported expression on line %(line)s. "
            "Only literal values (strings, numbers, booleans, None, "
            "lists, tuples, dicts) are allowed."
        ),
        params={"line": lineno},
        code="unsupported_expression",
    )
