# ADR 2025-11-07: Assertion UI Refactor

## Status

Accepted

## Context

Authors currently pick assertion targets from a fixed select control that is
populated with the validator’s catalog entries. That keeps assertions aligned
with the catalog, but it also blocks early workflow design when a validator has
not been curated yet. Product feedback highlights two related needs:

1. Provide lightweight discovery of the existing catalog while editing an
   assertion.
2. Allow authors to reference data that the validator does not yet expose, so
   they can capture the intent and come back later to wire the source.

Any free-form entry must still be predictable for the execution engine. We
already store target slugs as strings, so we can accept user-provided paths if
they follow a consistent, JSON-oriented grammar (dot for nested objects, square
brackets for numeric indices).

We also have validators where arbitrary targets should be forbidden because the
provider requires explicit catalog definitions (for example tightly scoped
EnergyPlus engines). The UI therefore needs a toggle so validator owners can opt
in to free-form targets per validator.

## Decision

- Replace the `<select>` used for assertion targets with an `<input>` bound to
  an HTML `datalist`. The input lists every catalog entry label/slug for the
  current validator, but remains a text field so authors may enter new values.
- Introduce a boolean field on validators,
  `allow_custom_assertion_targets` (default `False`), that controls whether the
  form accepts values outside the catalog.
- When the flag is off, the server validates that the submitted target must be a
  catalog entry. When the flag is on, the user may either choose an entry
  (`target_catalog_entry`) or type a free-form JSON-style path (`target_field`;
  dot-notation with `[index]` arrays). A check constraint ensures exactly one of
  these fields is populated for every assertion.
- Update `AssertionType` so we only have the coarse modes `BASIC` (structured
  operator-based assertions) and `CEL_EXPRESSION` (raw CEL text). Future types
  can extend this enum without another schema change.
- Validators remain immutable once a step is created. If an author wants to try
  a different validation engine they should create a new step; this keeps
  existing rulesets, catalogs, and audit history attached to the original
  validator.
- BASIC assertions always store their target in `target_catalog_entry` or
  `target_field`, set `operator`, and persist the operator payload in `rhs`
  (values) plus `options` (inclusive bounds, tolerance, etc.).
- CEL_EXPRESSION assertions keep the target inputs but remove the operator UI in
  favor of a single CEL textarea. They store the expression under
  `rhs={"expr": "<raw cel>"}` with `operator=Operator.CEL_EXPR`. A checkbox lets
  authors control whether free-form target names are allowed when validating the
  CEL.
- Add inline help text so authors know that custom targets must follow the
  JSON-style dot/bracket grammar.
- Refactor the “Add Assertion” form as defined below.

## Consequences

- Authors get autocomplete-like suggestions for existing catalog entries while
  still being able to type new targets when a validator opts in.
- Providers that require curated catalogs are shielded by leaving the flag
  disabled; they continue to reject unknown targets.
- We need a follow-up ergonomics pass to surface a management view that
  highlights custom assertion targets so validator owners can backfill catalog
  entries.
- Tests covering assertion creation must exercise both acceptance paths (catalog
  constrained vs. free-form).

## Updating UI

Change the "Add Assertion" dialog in the following way:

- Step 1: `Assertion Type` toggle (radio group) with `Basic` (default) and
  `CEL expression`.
- Step 2: `Target Field`, always visible. This is the datalist-backed input that
  lists catalog entries but still accepts free text. Show help text describing
  the JSON-style grammar for custom paths (dot notation and `[index]`). When the
  validator disallows custom targets the field is limited to the catalog list.
- Step 3a (Basic assertions): show a `Condition` select using the Operator
  TextChoices. Changing the operator reveals the relevant fields that map to
  `rhs` and `options`:
  - ≤ / ≥ / < / > / = / ≠ : single Value input.
  - Between : Min, Max, checkboxes `include min` / `include max`.
  - Is one of / Is not one of : textarea (newline separated), checkbox
    `case-insensitive`.
  - Contains / Starts / Ends with : text input, checkbox `case-insensitive`.
  - Matches regex : regex field + optional “Test pattern” helper.
  - Approx equals : value + tolerance input with toggle for abs/%.
  - Before / After : date-time picker.
  - Any / All / None : minimal nested editor (operator + value) so collections
    can be checked.
- Step 3b (CEL assertions): hide the structured fields and show a single CEL
  textarea plus a checkbox (`Allow custom signal names`, default checked). When
  unchecked, CEL validation must enforce that every identifier resolves to a
  catalog entry.

Shared options (render only when relevant):

- checkbox: Case-insensitive
- checkbox: Unicode/locale fold
- checkbox: Coerce types (e.g., “123” → 123)
- checkbox: Treat missing as null

Add a preview read-only section that shows the complete CEL expression that the
structured form produces.

Key model changes (summary)

- Store the target in two complementary fields:
  - `target_catalog_entry` (FK to `ValidatorCatalogEntry`) for curated catalog slugs.
  - `target_field` (free-text JSON-style path) for user-entered targets.
  - A database constraint ensures exactly one is populated per row.
- Keep `AssertionType`, but collapse it to two coarse options:
  - `BASIC` → structured operator assertions.
  - `CEL_EXPRESSION` → raw CEL textarea.
- Introduce a normalized `operator` (`eq`, `le`, `between`, `cel_expr`, …) that
  decouples UI labels from storage. BASIC assertions set this value, while CEL
  assertions use `Operator.CEL_EXPR`.
- Split the generic definition JSON into two purpose-built blobs:
  - rhs (right-hand side) — the value(s) or pattern(s) for the operator.
  - options — inclusivity flags, tolerance, case-folding, units, etc.
- Add `cel_cache` (optional) to store the generated CEL for preview/debug.
  - For operator="cel_expr", store the raw CEL in `rhs = {"expr": "..."}` and
    optionally copy it into `cel_cache`.
- Keep `when_expression` as a CEL guard and compose it with the rendered
  operator expression at evaluation time.

### Target validation

`target_field` values must follow a predictable grammar so engines can resolve
them safely. We will accept identifiers that:

- start with a letter or underscore,
- may include numbers, underscores, or dashes (`[A-Za-z_][A-Za-z0-9_-]*`),
- use dots to traverse nested dictionaries (`data.errors.primary`),
- use `[index]` to address list items (`results[0].value`).

Anything outside that grammar (whitespace, wildcards, quoted keys, etc.) is
rejected at form-validation time with actionable help text. The same pattern is
referenced in the UI help copy so expectations stay aligned.

### CEL validation

CEL assertions pass through two validation layers:

1. Structural checks ensuring the string is non-empty with balanced delimiters.
2. If “Allow custom signal names” is unchecked (or the validator forbids them),
   every slug referenced via helper functions such as `series("slug")` must exist
   in the catalog. Unknown slugs raise a form error before we persist anything.

The normalized CEL (with optional guard) is cached in `cel_cache` so the UI can
render previews without recompiling the expression.

Here's one idea for the model:

```
class RulesetAssertion(models.Model):
    """
    A single assertion bound to a ruleset.

    Either `target_catalog_entry` (FK) or free-text `target_field` must be provided,
    but not both.
    """
    class Meta:
        ordering = ["order", "pk"]
        constraints = [
            # Exactly one of target_catalog_entry or target_field must be set
            models.CheckConstraint(
                name="ck_assertion_target_oneof",
                condition=(
                    (Q(target_catalog_entry__isnull=False) & Q(target_field__exact=""))
                    |
                    (Q(target_catalog_entry__isnull=True) & Q(target_field__gt=""))
                ),
            ),
            models.Index(fields=["ruleset", "order"]),
            models.Index(fields=["operator"]),
        ]

    ruleset = models.ForeignKey(
        "Ruleset",
        on_delete=models.CASCADE,
        related_name="assertions",
    )
    order = models.PositiveIntegerField(default=0)

    # NEW: normalized operator
    operator = models.CharField(
        max_length=32,
        choices=Operator.choices,
        default=Operator.LE,
    )

    # Target can be catalog-backed OR arbitrary field
    target_catalog_entry = models.ForeignKey(
        "ValidatorCatalogEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assertions",
        help_text=_("Reference a known catalog entry (preferred when available)."),
    )
    target_field = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "Field path to validate (e.g., 'metrics.eui'). "
            "Used when not referencing a catalog entry."
        ),
    )

    severity = models.CharField(
        max_length=16,
        choices=Severity.choices,
        default=Severity.ERROR,
    )

    # Optional CEL guard
    when_expression = models.TextField(
        blank=True, default="",
        help_text=_("Optional CEL condition that must be true to evaluate this assertion.")
    )

    # Operator-specific payloads
    rhs = models.JSONField(
        blank=True, default=dict,
        help_text=_("Right-hand side payload for the operator (value/min+max/list/pattern/etc.).")
    )
    options = models.JSONField(
        blank=True, default=dict,
        help_text=_("Operator options (inclusive_min/max, tolerance, case_insensitive, units, etc.).")
    )

    # Presentation
    message_template = models.TextField(
        blank=True, default="",
        help_text=_("Failure message. Supports {{field}}, {{actual}}, {{value}}, {{min}}, {{max}}, {{tolerance}}, {{units}} with filters (round, upper, lower, default).")
    )

    # Diagnostics / forward-compat
    cel_cache = models.TextField(blank=True, default="")
    spec_version = models.PositiveIntegerField(default=1)

    def __str__(self):
        t = self.target_catalog_entry or self.target_field or "?"
        return f"{self.ruleset_id}:{self.operator}:{t}"

    # Back-compat loader (optional): map old AssertionType to new operator+rhs
    @classmethod
    def from_legacy(cls, *, ruleset, assertion_type, target_slug, definition, **kwargs):
        op, rhs, options = map_legacy(assertion_type, definition)
        return cls(
            ruleset=ruleset,
            operator=op,
            target_field=target_slug,  # legacy stored in slug; now free text
            rhs=rhs, options=options, **kwargs
        )

    def clean(self):
        super().clean()
        # one-of target check is enforced by constraint, but give a friendly error on form save
        if bool(self.target_catalog_entry) == bool((self.target_field or "").strip()):
            raise ValidationError(
                {"target_field": _("Select a catalog entry OR enter a field path, not both.")}
            )
```

Notes:

- For operator="cel_expr", put the user’s CEL in rhs = {"expr": "<cel here>"}.
- For operator="between", store rhs = {"min": 0, "max": 50} and options = {"inclusive_min": true, - "inclusive_max": false}.
- For operator="in", store rhs = {"values": ["A","B","C"]}.
- Keep when_expression as your “guard.”

Keeping catalog + arbitrary fields happy

- Authoring UI: a radio/toggle:
- ( ) Catalog → select ValidatorCatalogEntry
- ( ) Custom field → free-text target_field
- At evaluation time:
  - Prefer target_catalog_entry.binding_config/metadata for type hints/units if present.
  - Else use target_field as a direct path.

Generating CEL (no round-trip)

- Implement a small renderer that turns any assertion record into CEL:
  -Cache the output into cel_cache if you want quick “preview” rendering.

```
def cel_literal(v):
    if isinstance(v, str): return f'"{v}"'
    if v is None: return "null"
    if isinstance(v, bool): return "true" if v else "false"
    return str(v)

def to_cel(assertion: RulesetAssertion, *, left_prefix="doc"):
    left = f"{left_prefix}.{assertion.target_catalog_entry.slug}" if assertion.target_catalog_entry_id else f"{left_prefix}.{assertion.target_field}"
    r = assertion.rhs or {}
    o = assertion.options or {}
    op = assertion.operator

    if op == Operator.LE:
        expr = f"{left} <= {cel_literal(r['value'])}"
    elif op == Operator.BETWEEN:
        min_cmp = ">=" if o.get("inclusive_min", True) else ">"
        max_cmp = "<=" if o.get("inclusive_max", True) else "<"
        expr = f"({left} {min_cmp} {r['min']} && {left} {max_cmp} {r['max']})"
    elif op == Operator.IN:
        vals = ", ".join(cel_literal(v) for v in r.get("values", []))
        expr = f"{left} in [{vals}]"
    elif op == Operator.MATCHES:
        expr = f"re.matches({cel_literal(r['pattern'])}, {left})"
    elif op == Operator.CEL_EXPR:
        expr = r.get("expr", "")
    # ...handle others...
    else:
        raise NotImplementedError(op)

    if assertion.when_expression:
        expr = f"({assertion.when_expression}) && ({expr})"
    return expr
```
