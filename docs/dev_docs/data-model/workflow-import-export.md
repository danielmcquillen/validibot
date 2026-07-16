# Workflow import and export (the `.vaf` archive)

This page is the developer-facing reference for moving a workflow between
organisations or instances: the `.vaf` archive format, the definition schema, the
per-validator serialization architecture, and how to add import/export support to
a new validator.

It is the close cousin of [Workflow versioning and the trust
contract](workflow-versioning.md). Cloning copies a workflow's whole graph to a
new version *inside* one org; import/export does the same copy but to a *portable
file* and back. The two deliberately share the same create-order and
foreign-key-rebinding rules so they can't drift —
`WorkflowVersioningService.clone()` is the canonical reference for "rebuild a
workflow from its parts".

The decision record is
[ADR-2026-03-31: Workflow Export and Import](https://github.com/validibot/validibot-project)
(private). The user-facing how-to lives in the marketing repo's user docs.

## Why a format at all

A workflow is a graph, not a row: a sequence of steps, each with a validator
reference, a ruleset and its assertions, step I/O definitions and input bindings,
derivations, resources, plus workflow-level signal mappings and a public-info
page. To hand that to another org or instance you need a self-contained snapshot
that carries the *shape* — never database IDs, never the owning org/user. Those
are minted or rebound at import time.

## The `.vaf` archive

A `.vaf` (Validibot Archive Format) file is an ordinary ZIP containing:

| Member | Purpose |
|---|---|
| `manifest.json` | Metadata about the *archive*: `vaf_version`, `kind`, provenance (`exported_at`, `exported_by`, `validibot_version`), and the member list. |
| `workflow.json` | The workflow **definition** — its own `format_version` and the full graph. |
| `files/<sha256>` | Binary blobs referenced from `workflow.json` by content hash: step-owned resources (FMU models, weather files, templates) **and** uploaded ruleset schema files (a JSON/XML schema upload stores its schema in `rules_file`, not `rules_text`, so the bytes are bundled here and restored on import). |

Two version numbers do different jobs: `manifest.vaf_version` versions the
*container*, and `workflow.json`'s `format_version` versions the *definition
schema*. Import refuses an archive whose `vaf_version` it doesn't understand, and
a definition whose `format_version` it doesn't understand.

### Bare JSON is allowed — until it isn't

Export always produces a `.vaf`. But import also accepts a bare `workflow.json`,
because the common case (the Darwin Core example, most inline-validator
workflows) bundles no files. The rule, enforced on import: a definition that
references bundled files **cannot** be imported as bare JSON — the bytes aren't
there, so it fails with a clear "please import the `.vaf`" error. This is why the
archive format exists from day one even though it usually wraps a single file:
the moment a workflow has an uploaded resource, JSON alone can't carry it.

The packaging layer (`workflows/services/io/vaf.py`) is pure and defensive:
size caps (50 MB compressed, 200 MB inflated), only known members are read,
absolute/`..` paths are rejected, and every `files/<hash>` member is verified to
actually hash to its name so a tampered archive can't smuggle mismatched bytes.

## The definition schema (`workflow.json`)

```json
{
  "format_version": 2,
  "workflow": {
    "name": "Darwin Core Occurrence QA",
    "slug": "darwin-core-occurrence-qa",
    "allowed_file_types": ["TEXT"],
    "public_info": null,
    "signal_mappings": [],
    "...": "the workflow contract fields (history_policy, retention, agent_*, ...)"
  },
  "steps": [
    {
      "order": 10,
      "step_key": "check_incoming_csv",
      "name": "Check incoming CSV",
      "config": {"delimiter": ",", "encoding": "utf-8", "has_header": true},
      "display_settings": {"delimiter_label": "Comma", "column_count": 2},
      "kind": "validator",
      "validator_ref": {
        "validation_type": "TABULAR",
        "slug": "tabular-validator",
        "version": 1,
        "is_system": true
      },
      "ruleset": {
        "name": "Darwin Core Occurrence Table Schema",
        "ruleset_type": "TABULAR",
        "rules_text": "{ ...the Table Schema... }",
        "metadata": {},
        "assertions": [ { "assertion_type": "cel_expr", "rhs": {"expr": "..."}, "options": {"tabular_stage": "row"}, "...": "..." } ]
      },
      "step_io_definitions": [],
      "input_bindings": [],
      "derivations": [],
      "io_promotions": [],
      "resources": []
    }
  ]
}
```

The exact field sets are enumerated once in `workflows/services/io/schema.py`
(`WORKFLOW_SCALAR_FIELDS`, `STEP_SCALAR_FIELDS`, `STEP_IO_DEFINITION_FIELDS`, …) so
the exporter and importer can never disagree about which fields make up the
definition.

## Architecture: generic graph + per-validator body

The split is the heart of the design, and it's what the phrase "each validator
knows how to serialize and deserialize its own description" means.

- **The generic exporter/importer** (`workflows/services/io/exporter.py`,
  `importer.py`) own the parts that are the same for every workflow: the workflow
  contract fields, steps, step-owned I/O definitions/input bindings/derivations/promotions,
  resources, public info, and signal mappings.
- **A per-validator `StepSerializer`** owns the part that is validator-specific:
  the step's *ruleset body* (rules + metadata + assertions). It lives in
  `validations/validators/base/step_serializer.py`, and a validator opts into a
  custom one by setting `step_serializer_class` on its `ValidatorConfig`.

The base `StepSerializer` already round-trips the common shape every inline
validator uses (`rules_text` + `metadata` + an ordered assertion list), so most
validators need *nothing*. The Tabular Validator is the one that subclasses it —
see below.

### Export flow

`export_definition(workflow)` returns `(definition_dict, files)`. It walks the
prefetched step graph, asks each step's serializer to `export_ruleset(...)`, and
collects step-owned file bytes into `files` keyed by content hash.
`export_to_vaf(workflow, ...)` then packs that into archive bytes, stamping
provenance into the manifest.

### Import flow

`import_definition(definition, files=..., org=..., user=...)` runs inside one
`transaction.atomic()` and mirrors the cloner's create-order exactly, because
**assertions can target step-owned I/O definitions that don't exist until their step is
created**. Per step:

1. create the ruleset *row* (no assertions yet),
2. create the step (referencing the ruleset and the resolved validator),
3. create step-owned I/O definitions,
4. build a per-step I/O-definition resolver,
5. create the assertions (re-binding any step-I/O targets), then run the
   validator's `validate_imported_ruleset(...)` hook,
6. create bindings, derivations, and io-promotions,
7. restore resources (bundled files; warn on un-matchable catalog refs).

Then the workflow-level public info and signal mappings are created.

### What's minted or rebound on import

| Thing | At import |
|---|---|
| `uuid` | Fresh (model default) |
| `slug` | From the definition, suffixed `-2`, `-3`, … on collision in the target org |
| `version` | `1` |
| `is_active` | `True` — imported workflows are active and launchable immediately (deactivate if not wanted). An inactive workflow reads as archived in the list, which blocked import-then-run, so imports are not made inactive. |
| `workflow_visibility`, `make_info_page_public`, `x402_enabled`, `mcp_enabled` | **Forced to the locked state** (`workflow_visibility=PRIVATE`; the rest `False`). External exposure is never inherited on import — these toggles aren't even serialized, *and* the importer forces them locked (defense in depth against a hand-edited definition). Because imports are now active, inheriting wider visibility or agent access would auto-expose a workflow in the importing org; an import is private until its owner opts in. |
| `org` / `user` / `project` | The importing user's org/user; project unset |
| Ruleset name | Suffixed ` (2)`, ` (3)`, … to satisfy the `(org, type, name, version)` key |
| Validator | **Resolved**, never created (see below) |

Every created row is `full_clean()`-validated, so a malformed definition fails
loudly rather than producing a broken workflow.

## Validator resolution and warnings

A step references a validator; the importer resolves it on the target system:

- **Built-in (system) validators** resolve by `validation_type` — portable across
  installs even when slugs or versions differ.
- **Custom (org-authored) validators** resolve by `(validation_type, slug)` in the
  importing org.

A version that doesn't match the requested one is a **warning** (resolve to the
newest available, and tell the user). A validator that can't be resolved at all is
a **hard error** — a step with no validator can't run, so a partial import would be
worse than a clear failure.

Import returns an `ImportResult(workflow, warnings, components)`. Warnings are
non-fatal issues surfaced on the results page:

- validator version mismatches,
- shared catalog resources that couldn't be matched on the target (warn + skip;
  the step is flagged as needing the file),
- role-based access grants (not recreated — org-specific permission config the
  importer leaves to the user).

## Full-fidelity coverage and what's deferred

Import/export covers the same graph the cloner copies: workflow contract fields,
steps, rulesets + assertions, step-owned I/O definitions, input bindings,
derivations, io-promotions, step resources (catalog refs + bundled files), public
info, and signal mappings.

Deferred, with explicit handling:

- **Action steps** — import raises a clear "not yet supported" error rather than
  dropping them.
- **Role-based access grants** — warned, not recreated.
- **REST API endpoints, two-phase preview, bulk export, featured-image export, CLI
  support** — follow-ups (see the ADR).

## Adding import/export support to a new validator

For most validators: **do nothing.** The base `StepSerializer` round-trips
`rules_text` + `metadata` + assertions, which is everything an inline validator's
ruleset is.

Override only when the validator has an invariant the model layer doesn't enforce
but the step editor does — the Tabular Validator is the worked example. Its row
assertions may only reference columns declared in the Table Schema; that check
lives in the step-editor form, which import bypasses. So it ships a serializer:

```python
# validations/validators/tabular/serializer.py
class TabularStepSerializer(StepSerializer):
    def validate_imported_ruleset(self, ruleset, body):
        declared = self._declared_columns(ruleset.rules_text)
        for assertion in ruleset.assertions.all():
            if (assertion.options or {}).get("tabular_stage") != "row":
                continue
            unknown = self._referenced_columns(...) - declared
            if unknown:
                raise WorkflowImportError(..., code="vaf.tabular_unknown_column")
```

and registers it on its config:

```python
# validations/validators/tabular/config.py
config = ValidatorConfig(
    ...,
    step_serializer_class=(
        "validibot.validations.validators.tabular.serializer.TabularStepSerializer"
    ),
)
```

`get_step_serializer(validation_type)` resolves that dotted path lazily and caches
the instance, falling back to the base serializer when a validator declares none.
If your validator's *export* shape needs special handling too, override
`export_ruleset(...)` and the `create_ruleset_row(...)` / `create_assertions(...)`
methods — but reach for that only when the generic shape genuinely doesn't fit.

> **Note:** `step_serializer_class` is config-only plumbing, not a `Validator`
> model field. If you add a new `ValidatorConfig` field, remember to exclude it
> from the model dump in `sync_validators.py` (alongside `step_editor_cards`,
> `validator_class`, …), or `sync_validators` will fail.

## The Darwin Core example

A committed, regenerable fixture exercises the whole path end to end. It is a
single tabular step — the Darwin Core occurrence Table Schema plus the four row
rules (depth ordering, Null Island, presence-implies-count, positive uncertainty)
— and ships as both `tests/workflows/darwin_core.json` and `darwin_core.vaf`.

Regenerate after a schema change:

```bash
python manage.py build_darwin_core_example
```

The command builds the definition from real enum values and the real Table Schema
asset, so the fixture can't drift from what the importer expects. The packer is
deterministic, so regeneration is byte-stable.

## Tests

- `workflows/tests/test_vaf.py` — packaging: round-trip, bare JSON, tampered-hash,
  path traversal, version gating.
- `workflows/tests/test_workflow_io.py` — export → import round-trip into a fresh
  org, slug-collision suffixing, both committed fixtures, unresolved-validator
  hard error, version-mismatch warning, and the tabular column guard.
- `workflows/tests/test_workflow_io_views.py` — the import page, the
  success/error fragments, the permission gate, and the export download.

## See also

- [Workflow versioning and the trust contract](workflow-versioning.md) — the
  in-org cloning cousin whose create-order this shares.
- [Tabular Validator](tabular-validator.md) — the validator behind the example,
  and the one validator that ships a custom step serializer.
- [Assertions](assertions.md) — what an assertion's serialized fields mean.
