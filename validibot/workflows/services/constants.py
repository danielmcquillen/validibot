"""Service helpers for workflow Constants (the ``c.*`` / ``const.*`` namespace).

ADR-2026-06-18 adds **Constants** as a distinct workflow primitive: a
workflow-scoped, author-defined, *fixed* value referenced in assertions as
``c.<name>`` (long form ``const.<name>``). Unlike every other namespace, a
constant's value is known at authoring time — it comes from the workflow
*definition*, not the run — which is exactly what lets a signed credential
state "checked against ``c.energy_price = 0.40``" as a legible, separate fact.

This module is the **single home** for everything a constant needs that is not
the model itself:

* :func:`coerce_constant_value` — parse/validate a raw author-entered value
  against its declared type, returning the canonical *stored* form. ``NUMBER``
  is stored as a canonical decimal *string* (via :class:`~decimal.Decimal`) so
  ``0.40`` round-trips exactly through the digest, manifest, and credential.
* :func:`build_workflow_constants_context` — the runtime ``{name: value}`` map
  injected once into the assertion context (CEL ``c.*``/``const.*`` and the
  Basic evaluator's nested ``c``/``const`` sub-dict). Numeric constants are
  coerced to ``float`` here because CEL has no decimal type — evaluation is
  ``double``; only storage/attestation is exact.
* :func:`format_constant_display` — the author-facing "``0.40`` (number)"
  rendering used by the reference panel and autocomplete hints.
* :func:`validate_constant_name` / :func:`validate_constant_name_unique` — name
  rules. **Deliberately separate from the signal helpers**: a constant shares
  the reserved-root / valid-identifier checks but is unique only *among
  constants* (per-primitive uniqueness), and must NOT reuse
  ``validate_signal_name_unique`` (which enforces cross-producer uniqueness of
  ``s.<name>``). A constant sharing a bare name with a signal is allowed — the
  ``c.``/``s.`` prefix disambiguates.

Size/depth limits for structured (``LIST``/``OBJECT``) constants are enforced
here at save time, aligned to the runtime CEL context bounds so a constant can
never bloat the context, manifest, or digest.
"""

from __future__ import annotations

import json
from decimal import Decimal
from decimal import InvalidOperation
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext_lazy as _

from validibot.validations.constants import CEL_MAX_CONTEXT_DEPTH
from validibot.workflows.constants import WorkflowConstantType

if TYPE_CHECKING:
    from validibot.workflows.models import Workflow
    from validibot.workflows.models import WorkflowConstant

# ── Save-time bounds for structured constants ────────────────────────────────
# A constant is a *named threshold / allow-list*, not a dataset. These caps stop
# a pathological LIST/OBJECT from bloating the activation context, the evidence
# manifest, or the digest preimage. They are enforced in ``coerce_constant_value``
# (save time), not only at CEL runtime, so the contract is guaranteed before any
# run. The depth cap reuses the existing runtime bound so save-time and eval-time
# agree (see ``validations/constants.py``); the rest are deliberately small.
CONSTANT_MAX_SERIALIZED_BYTES = 8 * 1024
CONSTANT_MAX_DEPTH = CEL_MAX_CONTEXT_DEPTH
CONSTANT_MAX_LIST_LENGTH = 256
CONSTANT_MAX_OBJECT_KEYS = 64


class ConstantValueError(ValueError):
    """Raised when a constant's value does not satisfy its declared type/bounds.

    Carries a human-readable message suitable for surfacing on the ``value``
    field of the Add/Edit Constant form.
    """


def coerce_constant_value(data_type: str, raw: Any) -> Any:
    """Validate ``raw`` against ``data_type`` and return its canonical stored form.

    This is the one place that decides what a constant's *stored* value looks
    like. It is called from the model's ``clean()`` (and the form) so the
    constant's contract is enforced at save time, not discovered at run time.

    Returns:
        The JSON-serialisable canonical value to store in ``WorkflowConstant.value``:

        * ``STRING`` → ``str`` (taken literally; no quoting/JSON-parsing trap).
        * ``NUMBER`` → a canonical **decimal string** (e.g. ``"0.40"``), so exact
          value and author-chosen precision survive into the digest/credential.
        * ``BOOLEAN`` → ``bool``.
        * ``LIST`` → ``list`` (parsed from JSON if given a string), bounded.
        * ``OBJECT`` → ``dict`` (parsed from JSON if given a string), bounded.

    Raises:
        ConstantValueError: if ``raw`` cannot be coerced to ``data_type`` or
            violates a structured-value bound.
    """
    if data_type == WorkflowConstantType.STRING:
        # A constant is a committed literal — "EUR" means the string EUR, not a
        # JSON token to parse. Coerce non-strings via str() for forgiveness, but
        # the form always hands us a string.
        return raw if isinstance(raw, str) else str(raw)

    if data_type == WorkflowConstantType.NUMBER:
        return _coerce_number(raw)

    if data_type == WorkflowConstantType.BOOLEAN:
        return _coerce_boolean(raw)

    if data_type == WorkflowConstantType.LIST:
        value = _maybe_load_json(raw, expected="list")
        if not isinstance(value, list):
            raise ConstantValueError(
                _('Expected a JSON list (e.g. ["EUR", "GBP"]).'),
            )
        _enforce_structured_bounds(value)
        return value

    if data_type == WorkflowConstantType.OBJECT:
        value = _maybe_load_json(raw, expected="object")
        if not isinstance(value, dict):
            raise ConstantValueError(
                _('Expected a JSON object (e.g. {"min": 1, "max": 9}).'),
            )
        _enforce_structured_bounds(value)
        return value

    raise ConstantValueError(
        _("Unknown constant type '%(data_type)s'.") % {"data_type": data_type},
    )


def _coerce_number(raw: Any) -> str:
    """Coerce ``raw`` to a canonical decimal string, preserving precision.

    We round-trip through :class:`~decimal.Decimal` so ``"0.40"`` stays
    ``"0.40"`` (a JSON float would collapse it to ``0.4`` and could not
    represent ``0.1`` exactly). The stored form is the *string* — exact for the
    digest/credential — and is coerced to ``float`` only when building the CEL
    context (CEL has no decimal type).
    """
    if isinstance(raw, bool):  # bool is a subclass of int — reject explicitly
        raise ConstantValueError(_("Expected a number, got a boolean."))
    text = str(raw).strip()
    if not text:
        raise ConstantValueError(_("A number value is required."))
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ConstantValueError(
            _("'%(value)s' is not a valid number.") % {"value": text},
        ) from exc
    if not dec.is_finite():
        raise ConstantValueError(_("Number must be finite (no NaN/Infinity)."))
    # ``str(Decimal("0.40"))`` preserves the trailing zero; normalise only the
    # exponent representation Decimal may emit (e.g. "1E+2") to plain digits.
    return _format_decimal(dec)


def _format_decimal(dec: Decimal) -> str:
    """Render a Decimal as a plain (non-scientific) string, keeping precision."""
    # Avoid scientific notation ("1E+2") so the stored/attested form is the
    # human-written one. ``f"{dec:f}"`` formats in fixed-point without losing
    # the significant trailing zeros the author typed.
    return f"{dec:f}"


def _coerce_boolean(raw: Any) -> bool:
    """Coerce ``raw`` to a bool, accepting the form's true/false strings."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ConstantValueError(_("Expected a boolean (true or false)."))


def _maybe_load_json(raw: Any, *, expected: str) -> Any:
    """Return ``raw`` parsed from JSON when it's a string, else ``raw`` as-is.

    The form's List/Object editor submits JSON text; programmatic callers may
    pass an already-parsed ``list``/``dict``. Both paths converge here.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConstantValueError(
                _("Could not parse JSON %(expected)s: %(error)s")
                % {"expected": expected, "error": exc.msg},
            ) from exc
    return raw


def _enforce_structured_bounds(value: Any) -> None:
    """Reject oversized/over-deep structured constants at save time.

    See the module-level ``CONSTANT_MAX_*`` caps for the rationale: a constant
    is a named threshold/allow-list, so it must stay small enough that it can't
    bloat the activation context, manifest, or digest.
    """
    serialized = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(serialized.encode("utf-8")) > CONSTANT_MAX_SERIALIZED_BYTES:
        raise ConstantValueError(
            _("Structured constant is too large (max %(kb)d KiB serialized).")
            % {"kb": CONSTANT_MAX_SERIALIZED_BYTES // 1024},
        )
    if isinstance(value, list) and len(value) > CONSTANT_MAX_LIST_LENGTH:
        raise ConstantValueError(
            _("List constant has too many items (max %(n)d).")
            % {"n": CONSTANT_MAX_LIST_LENGTH},
        )
    if isinstance(value, dict) and len(value) > CONSTANT_MAX_OBJECT_KEYS:
        raise ConstantValueError(
            _("Object constant has too many keys (max %(n)d).")
            % {"n": CONSTANT_MAX_OBJECT_KEYS},
        )
    depth = _max_depth(value)
    if depth > CONSTANT_MAX_DEPTH:
        raise ConstantValueError(
            _("Structured constant is nested too deeply (max depth %(n)d).")
            % {"n": CONSTANT_MAX_DEPTH},
        )


def _max_depth(value: Any, _current: int = 1) -> int:
    """Return the maximum nesting depth of a JSON-shaped value."""
    if isinstance(value, dict):
        if not value:
            return _current
        return max(_max_depth(v, _current + 1) for v in value.values())
    if isinstance(value, list):
        if not value:
            return _current
        return max(_max_depth(v, _current + 1) for v in value)
    return _current


def value_for_cel(constant: WorkflowConstant) -> Any:
    """Return a constant's value coerced to its CEL-ready runtime form.

    Storage and evaluation have different fidelity needs (ADR-2026-06-18):
    storage is exact (decimal string for NUMBER), but CEL has only
    ``int``/``uint``/``double``, so a numeric constant is evaluated as a
    ``double``. We therefore convert the stored decimal string to ``float``
    (or ``int`` when integral, so ``c.count == 3`` compares as an int) here,
    at the boundary between the stored contract and the evaluator.
    """
    data_type = constant.data_type
    stored = constant.value
    if data_type == WorkflowConstantType.NUMBER:
        try:
            dec = Decimal(str(stored))
        except (InvalidOperation, ValueError):
            return stored
        # Integral decimals (no fractional part) → int so CEL int-comparisons
        # behave; everything else → float (CEL ``double``).
        if dec == dec.to_integral_value():
            return int(dec)
        return float(dec)
    # STRING / BOOLEAN / LIST / OBJECT store their CEL-ready form directly.
    return stored


def build_workflow_constants_context(workflow: Workflow | None) -> dict[str, Any]:
    """Build the ``{name: cel_ready_value}`` map for a workflow's constants.

    This is the runtime projection injected once into the assertion context —
    bound as both ``c`` and ``const`` in the CEL context and as a nested
    ``c``/``const`` sub-dict in the Basic evaluator's enriched payload. It needs
    no submission data (a constant is workflow-definition-derived), so unlike
    signal resolution it never touches the run.

    Returns an empty dict when ``workflow`` is ``None`` or has no constants.
    """
    if workflow is None:
        return {}
    workflow_pk = getattr(workflow, "pk", None)
    if not isinstance(workflow_pk, int):
        return {}

    from validibot.workflows.models import WorkflowConstant

    context: dict[str, Any] = {}
    for constant in WorkflowConstant.objects.filter(workflow=workflow).order_by(
        "name",
    ):
        context[constant.name] = value_for_cel(constant)
    return context


def format_constant_value(constant: WorkflowConstant) -> str:
    """Render just a constant's VALUE for display (no name/type decoration).

    Structured LIST/OBJECT values render as JSON (``["EUR", "GBP"]``), not the
    Python ``repr`` (``['EUR', 'GBP']``) that ``{{ c.value }}`` would print;
    BOOLEAN renders as ``true``/``false``; scalars use the stored string so
    NUMBER precision (``0.40``) is preserved verbatim. Used by templates via the
    ``WorkflowConstant.display_value`` property.
    """
    stored = constant.value
    if constant.data_type in {
        WorkflowConstantType.LIST,
        WorkflowConstantType.OBJECT,
    }:
        return json.dumps(stored, separators=(", ", ": "), ensure_ascii=False)
    if constant.data_type == WorkflowConstantType.BOOLEAN:
        return "true" if stored else "false"
    return str(stored)


def format_constant_display(constant: WorkflowConstant) -> str:
    """Render a constant for the author reference panel / autocomplete hint.

    Example: ``c.energy_price = 0.40 (number)``. Uses the *stored* value so
    NUMBER precision (``0.40``, not ``0.4``) is shown verbatim.
    """
    type_label = str(
        WorkflowConstantType(constant.data_type).label,
    ).lower()
    return f"c.{constant.name} = {format_constant_value(constant)} ({type_label})"


def validate_constant_name(name: str) -> list[str]:
    """Validate a constant name; return a list of error messages (empty = OK).

    A constant name must be a valid CEL identifier and must not be a reserved
    namespace root (the reserved set now includes ``c``/``const``). This shares
    the identifier/reserved rules with signals — by reusing the *same*
    ``validate_signal_name`` checks — but uniqueness is handled separately
    (see :func:`validate_constant_name_unique`).
    """
    from validibot.validations.services.signal_resolution import validate_signal_name

    errors = list(validate_signal_name(name))
    # ``validate_signal_name`` phrases its messages as "signal name"; re-word
    # to "constant name" so form errors read correctly for this primitive.
    return [
        e.replace("signal name", "constant name").replace(
            "as a signal",
            "as a constant",
        )
        for e in errors
    ]


def validate_constant_name_unique(
    workflow_id: int,
    name: str,
    *,
    exclude_constant_id: int | None = None,
) -> list[str]:
    """Check that ``name`` is unique **among constants** in the workflow.

    Deliberately a separate, constant-scoped helper — it must NOT reuse or
    generalise ``validate_signal_name_unique``, which enforces uniqueness across
    three *producers* of ``s.<name>`` (workflow mappings, in-row promotions,
    overlay promotions). A constant has a single producer (``WorkflowConstant``),
    and cross-primitive collisions are allowed (``c.energy_price`` and
    ``s.energy_price`` may coexist — the prefix disambiguates), so this checks
    only per-constant uniqueness.

    Returns an empty list if unique, else a list of error messages.
    """
    from validibot.workflows.models import WorkflowConstant

    qs = WorkflowConstant.objects.filter(workflow_id=workflow_id, name=name)
    if exclude_constant_id:
        qs = qs.exclude(pk=exclude_constant_id)
    if qs.exists():
        return [
            str(
                _("A constant named '%(name)s' already exists in this workflow.")
                % {"name": name},
            ),
        ]
    return []
