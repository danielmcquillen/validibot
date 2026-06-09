# Tabular Validator

The Tabular Validator validates tabular data — a table of typed rows — that
a user submits, such as a Darwin Core occurrence export or a building energy
meter CSV. Its primitive is the table, not the file format: CSV is simply the
reader that ships first. TSV, Excel, and Parquet are planned as future readers
in front of the same validation core, which is why the validator is called
"Tabular" rather than "CSV".

The full design lives in **ADR-2026-05-26** (`docs/adr/2026-05-26-csv-validator.md`
in the private `validibot-project` repo). This page documents the parts that are
implemented and how they fit together.

!!! note "Status: usable end-to-end"
    The validator is configurable and runnable through the UI: the
    reader/PREFLIGHT layer, native structured validation, **row-stage CEL
    assertions** (the `row.*` compiled-once-per-run loop), the registered
    `TabularValidator`, the full-screen **Expected columns** editor, and a
    step-detail **summary card** are all in place. After applying migrations and
    running `manage.py sync_validators`, an author can select **Tabular
    Validator**, configure columns manually or through import/inference, add
    `row.*` assertions, and run it. A `row.<column>` reference to a column not
    declared in the step's schema is rejected at save time (the ADR's
    column-existence obligation).

## How a table becomes an in-memory model

Reading a submission happens in two phases that are deliberately kept apart,
because conflating them hides real failures.

**PREFLIGHT** does the cheap checks that don't require loading the whole table.
It rejects an oversized file by its byte size before anything is decoded,
decodes the bytes (stripping a UTF-8 byte-order mark so it can't bleed into the
first column name), resolves the delimiter, and peeks at just the first record
to learn the column names and field count. PREFLIGHT never sees the body, so it
cannot catch a ragged row buried on line 40,000 — that's the next phase's job.
What PREFLIGHT guarantees is that reaching that failure is bounded: a 5 GB
upload is turned away before it can exhaust the worker.

**READ** parses the body into a [pandas](https://pandas.pydata.org/) dataframe.
Every cell is read as a string with no type or "NaN" inference, so an empty
cell is an empty string rather than a floating-point NaN, and parsing never
depends on the host's locale. Two operators on two machines read the same file
into the same values — which is what lets a downstream credential attest over a
reproducible result. Parsing is strict: a ragged row or an unbalanced quote
fails the read with a clear error rather than being silently repaired. This is
a quality-first validator, not a best-effort cleaner.

The result is the shared in-memory model every later layer consumes: a
string-valued dataframe keyed by canonical column names, plus the row count.
The row count is `len(df)` — the number of parsed rows — which is not the same
as counting newlines, because a quoted field may legitimately contain a newline.

## Delimiter resolution

The delimiter is decided, not guessed-and-hoped. If the settings declare one,
that declaration is authoritative. If nothing is declared, the reader sniffs
the delimiter from a bounded sample of the file. If a declaration and a sniff
disagree, the read fails with a clear message rather than silently preferring
one — an honest "you said comma, this looks tab-delimited" beats a wrong guess
that would mis-parse every row.

## Column names: one canonical name per column

Everything downstream — `row.*` keys, native column settings, the column-name
list exposed to assertions — uses a single logical name per column, resolved by
this precedence:

1. **Headered file** → the header string, after trimming whitespace. Headers
   must be safe to address: a blank name, a duplicate name, or two names that
   collide ignoring case (`Lat` vs `lat`) all fail by default, because
   `row.*` keys come from these names and an ambiguous header would make a
   reference silently wrong.
2. **Headerless file with a declared name for that position** → the declared
   name (aligned by position).
3. **Headerless file with no declared name** → a synthesised `column_1`,
   `column_2`, … (1-based).

So `column_N` is the headerless *default*, not a competing identity: declare a
name and it replaces the default. A headerless file is a first-class input —
the row namespace is always populated; the only difference is where the names
come from.

## Native structured validation

The structured constraints a non-coder declares in the settings — required
columns, per-column type, numeric range, string length, regex, enum, and
uniqueness — are checked **natively** against the dataframe. They are never
compiled to CEL; CEL is the separate lane for cross-field and conditional logic
the settings can't express. The two lanes share one finding stream.

The settings are stored as a [Frictionless Table
Schema](https://specs.frictionlessdata.io/table-schema/) descriptor. Validibot
adopts that *vocabulary* — `fields` with `type` and `constraints`, plus
`primaryKey` — but does not depend on the `frictionless` library; a descriptor
is parsed into a small internal schema model. An unrecognised type degrades to
`string` so an imported descriptor still loads.

**Where the standard is documented.** Table Schema's current canonical home is
[datapackage.org](https://datapackage.org/) (the linked `specs.frictionlessdata.io`
page is the v1 spec it grew from); both document the same
`fields`/`type`/`constraints` vocabulary we consume, so either is a valid
reference for authoring a descriptor. For *why* we adopt the vocabulary rather
than the `frictionless` processor — and why the National Archives CSV Schema
Language and W3C CSVW were considered and set aside — see **ADR-2026-05-26**,
section *Standards alignment: Frictionless Table Schema* and its *Alternatives
considered* subsection (`docs/adr/2026-05-26-csv-validator.md` in the private
`validibot-project` repo).

There are three ways to populate that descriptor: edit columns directly,
paste/import an existing descriptor, or **infer one from a sample file** (the
fastest common path — most users have a CSV, not a hand-written descriptor).
Import and inference populate the same ordered Django formset used for manual
editing. Inference (`infer.py`) reads a bounded sample, resolves the dialect and
column names through the normal reader, and guesses each column's type from its
values using the *same* coercion the validator enforces (so an inferred type
means what validation will check).
Candidate order is deliberate — `integer` before `boolean` (so `0`/`1` reads as
integer), and a column whose values don't all fit one type stays `string`.
Inference produces a *starting point* the author tightens: it picks types only,
never invents `min`/`max`/`enum` constraints.

Each cell is coerced from its string form to the declared type, deterministically
and locale-free: an empty cell is null, `"1,000"` is not a number, `"5.0"` is
not an integer, and dates are ISO 8601 only. This is the same coercion the
row-stage CEL layer will use, so the two never disagree about what a cell *is*.

Findings follow the reporting shape the whole validator uses: **one finding per
failed check** (per column), carrying the total failure count and up to
`report_max_examples` sample row numbers (10 by default) — never one finding per
failing row, so
a column with a million bad cells produces one readable finding, not a million.

### Uniqueness semantics

Uniqueness is a native V1 check (single `unique` columns and single/composite
`primaryKey`), computed over the *canonical typed values* — so `"1"` and `"1.0"`
are the same key in a numeric column. The null rules are pinned:

- **`unique`** follows SQL: nulls are exempt. Multiple missing values do not
  collide; only repeated *non-null* values are a violation.
- **`primaryKey`** is unique **and** non-null. A null in any key component is its
  own violation (`tabular.primary_key_null`), independent of duplicate detection;
  a composite key is keyed on the tuple of its parts.

### A note on blank lines

A wholly blank line (a trailing newline, an editor artifact) is skipped rather
than counted as an all-null data row — so it neither inflates `num_rows` nor
produces spurious "required value missing" findings. An empty *field* (`,2`) is
different: it is kept as a null cell and is subject to the `required` rule.

## The validator

`TabularValidator` ties the pieces together, mirroring how the JSON Schema
validator works. Its configuration lives on the ruleset: `ruleset.rules`
(`rules_text`/`rules_file`) holds the **Table Schema descriptor** (the
structured column config), and `ruleset.metadata` holds the **dialect**
(`delimiter`, `has_header`, `quotechar`) and `report_max_examples`.

A run does six things: load the schema (a schema that won't parse is a
`tabular.invalid_schema` finding, not a crash); read the body (a read failure
becomes a finding carrying its `tabular.*` code); run native validation, mapping
each `NativeFinding` onto a platform `ValidationIssue` with the count and sample
rows preserved in `meta`; run the **row-stage CEL** loop; run the
**column-stage CEL** aggregate assertions; and run the standard dataset (`i.*`)
/ output CEL assertion lane. The `i.*` dataset signals
(`num_rows`, `column_names`, `delimiter`, …) are exposed for those assertions and
returned for downstream steps.

The validator registers through the normal auto-discovery path: its `config.py`
declares a `ValidatorConfig` for `ValidationType.TABULAR`, and `sync_validators`
creates the DB row. CSV is carried as a plain-text submission
(`SubmissionDataFormat.CSV`).

## Row-stage CEL assertions

Cross-field and conditional row rules — `row.minimumDepthInMeters <=
row.maximumDepthInMeters`, `row.eventDate <= now()` — are CEL assertions
evaluated per row. They are stored as `RulesetAssertion` rows tagged
`options["tabular_stage"] == "row"`; the generic assertion lane skips those
(a `row.*`-only expression would otherwise misclassify and evaluate against an
unbound context), so the validator owns their evaluation.

**Authoring.** The assertion form accepts the `row.*` namespace **only on a
tabular step** (it's rejected elsewhere as an unknown identifier), and on save
it derives the stage from the expression: a `row.*` reference tags the assertion
`tabular_stage="row"`, a `col.*` reference tags it `"column"`, and anything else
(`i.*`/`s.*`) tags it `"dataset"`.
The form also checks that every `row.<column>` (dot or bracket) names a column
declared in the step's stored schema, rejecting a typo'd or absent column at
save time (skipped only when no schema is configured yet).

The engine (`row_eval.py`) follows the ADR's performance strategy: each
assertion's program is **compiled once per run**, then evaluated against every
row with **no per-eval thread**. Cost is bounded by the row/assertion caps, the
author-time expression-shape limits (checked at compile), and a single
wall-clock budget for the whole pass. Each row's values are bound into `row.*`
as their typed celpy values (via the shared `coercion.py`), alongside `s.*`
(workflow signals) and `i.*` (dataset metadata). `now()` is the **pinned run
clock** (`run.started_at`), not the wall clock — so a "not in the future" check
is reproducible for the run.

The determinism rule the engine enforces: an assertion that evaluates to `null`
or raises is a **failure** with a distinct code (`tabular.assertion_null` /
`tabular.assertion_error`), never a silent pass — a null cell must not satisfy a
comparison by comparing as null. A `false` result is the ordinary rule violation
(`tabular.row_assertion_failed`). Each assertion produces at most one finding per
outcome class, with a count and capped sample rows, so a million-row failure is
one readable finding.

## Column-stage CEL assertions

Column assertions run once after row validation against `col.*`. Each declared
column exposes aggregates computed from the same canonical typed values used by
native and row validation:

- `distinct_count`, `null_count`, `non_null_count`, and `null_ratio`
- `min` and `max`
- `sum` for `integer` and `number` columns

For example, `col.depth.null_ratio < 0.05` limits missing depth values, while
`col.temperature.max <= 60` caps the observed range. Empty cells and coercion
errors count as null. An optional declared column that is absent from the file
still has a stable empty aggregate, while actual column presence remains
available through `i.column_names`.

Column references are checked against the saved schema at author and import
time. Unknown aggregate names and `sum` on non-numeric columns are rejected
before the assertion is persisted. Null, non-boolean, and evaluation-error
results fail explicitly, matching the row-stage determinism contract.

## Configuring a step (the settings editor)

A tabular step is configured on the normal full-screen step settings page
(`steps/<id>/settings/`). `TabularStepConfigForm` owns the ordinary step and
dialect fields plus an ordered `TabularColumnFormSet`. Each column form exposes
the Table Schema field vocabulary: name/type, required, unique, primary-key
membership, numeric bounds, string length/pattern, and enum values. A narrow
Validibot extension, `x-validibot-requiredWhenPresent`, backs the no-CEL
**Required when another column exists** control. Encoding is
pinned to UTF-8 in V1 (not an editable field):
submitted content reaches the validator already decoded as UTF-8, so a per-step
encoding setting could only silently corrupt non-UTF-8 input — honoring other
encodings needs a raw-bytes read path, a future slice. On save,
`build_tabular_config` writes the descriptor to `ruleset.rules_text` and the
dialect to `ruleset.metadata` — the exact places the validator reads back at run
time, so the editor and the engine meet at the ruleset.

`build_tabular_descriptor()` converts the cleaned formset back into a
descriptor. It replaces the keys the editor owns while preserving unknown
top-level, field-level, and constraint-level metadata from an imported
descriptor. This prevents an edit to one range from stripping titles or
extension keys the UI does not expose. Primary-key checkboxes are serialized
in form order, and primary-key membership implies `required=true`.

Four HTMx endpoints support the authoring flow:

- `tabular/columns/` returns a correctly prefixed formset row and updates
  `TOTAL_FORMS` out of band.
- `tabular/schema/import/` validates pasted or uploaded JSON and returns a
  compatibility report plus a replacement preview.
- `tabular/schema/infer/` reads the bounded upload, returns a replacement
  preview, and updates resolved dialect controls out of band.
- `tabular/schema/apply/` validates the preview payload again and replaces the
  workspace only after explicit author confirmation.

The endpoints return server-rendered partials and never create temporary
database records. Invalid import/inference responses bind the posted formset
back into the replacement workspace, so unsaved column edits survive. The
ordinary POST path still accepts a pasted descriptor or sample upload without
HTMx, preserving progressive enhancement.

The formset enables Django's native ordering field. Up/down buttons reorder the
DOM and rewrite the hidden `ORDER` values; `build_tabular_descriptor()` uses
`ordered_forms`, so headerless-file position and composite-key order survive
the POST. Added rows receive focus after the HTMx swap. Request buttons use
`hx-disabled-elt`, `hx-indicator`, and `hx-sync` to prevent duplicate or
competing replacements.

Import compatibility is intentionally explicit. `schema.py` reports preserved
but unenforced features including `foreignKeys`, custom `missingValues`,
locale-specific number parsing, formats, unsupported scalar types, and unknown
standard constraints. The same notices appear in HTMx previews and as Django
messages on the no-JavaScript save path.

Saved descriptors can be downloaded from `tabular/schema/export/`. Encoding is
fixed to UTF-8 in V1 because the current submission API supplies decoded text;
presenting a selectable encoding before a raw-byte path exists would create a
setting the validator cannot honor.

The step-detail page shows a read-only **summary card** (reader, delimiter,
header, total/required columns, and dataset/row/column assertion counts) with
an "Edit settings" link. The existing assertion surface renders Tabular
assertions in stage-specific groups. Dataset and row groups each have a scoped
Add action, and the column group provides the same scoped flow for `col.*`.
A global Add action first asks which execution stage the rule belongs to.
The CEL editor provides stage-aware namespace hints and schema-derived
completions; row assertions also expose a per-assertion example-row limit.
Canceling Tabular settings returns to the workflow editor and focuses the
originating step card.

## Limits

"Human-scale, in-memory" is only a real contract if it's bounded, so the reader
enforces caps with safe, deployment-tunable defaults. Exceeding a cap fails the
run with a clear finding; it never silently truncates.

| Cap | Default | Enforced at |
|-----|---------|-------------|
| File size | 50 MB | PREFLIGHT (before the load) |
| Columns | 1,024 | PREFLIGHT (from the first record) |
| Rows | 1,000,000 | READ |

## Where the code lives

| Module | Responsibility |
|--------|----------------|
| `validations/validators/tabular/preflight.py` | PREFLIGHT: size/encoding/dialect/first-record checks, the `TabularDialect` and `TabularLimits` settings, and the `tabular.*` error codes |
| `validations/validators/tabular/readers/csv.py` | The CSV reader: column-name resolution, strict load into a dataframe, the row cap |
| `validations/validators/tabular/schema.py` | `TabularSchema` / `FieldSpec` model and the Table Schema descriptor parser |
| `validations/validators/tabular/infer.py` | Schema inference: read a sample, guess column types, return a descriptor + resolved dialect |
| `validations/validators/tabular/coercion.py` | Deterministic, locale-free coercion of a string cell to its declared type (shared with row-stage CEL) |
| `validations/validators/tabular/native.py` | Native structured validation: produces `NativeFinding`s for required/type/range/length/pattern/enum/uniqueness checks |
| `validations/validators/tabular/row_eval.py` | Row-stage CEL engine: compile-once-per-run, evaluate per row, typed `row.*` binding, null/error-as-failure, wall-clock budget |
| `validations/validators/tabular/column_eval.py` | Column-stage CEL engine: deterministic typed aggregates and one-shot `col.*` evaluation |
| `validations/validators/tabular/validator.py` | `TabularValidator`: reads the submission, runs native + row + column validation, maps findings to `ValidationIssue`, runs the dataset/output CEL lane |
| `validations/validators/tabular/config.py` | The `ValidatorConfig` that makes the validator discoverable and DB-syncable |
| `workflows/forms.py` (`TabularStepConfigForm`, `TabularColumnFormSet`) | Step settings, column validation, and metadata-preserving descriptor serialization |
| `workflows/views/steps.py` (`Tabular*View`) | HTMx row/import/inference endpoints and settings-page context |
| `workflows/views_helpers.py` (`build_tabular_config`) | Writes the descriptor to `ruleset.rules_text` and dialect to `ruleset.metadata` on save |
| `workflows/step_configs.py` (`TabularStepConfig`) | Typed display config for the step-detail summary card |
| `templates/workflows/partials/tabular_*` | Full-screen settings sections, schema workspace, and column-card partials |
| `static/src/ts/tabularSchema.ts` | Type-aware constraint visibility, deletion, and live column count |
| `templates/.../components/tabular_config_card.html` | The read-only step-detail summary card |

Read failures are raised as a `TabularReadError` carrying a machine-readable
`code` (always prefixed `tabular.`, e.g. `tabular.file_too_large`,
`tabular.parse_error`). Native validation returns `NativeFinding`s carrying the
same kind of `tabular.*` code (e.g. `tabular.out_of_range`,
`tabular.unique_violation`) plus a count and sample rows, so the validator can
emit structured findings without matching on message text.

## Import and export

The Tabular Validator is the one validator that ships a custom step serializer
for [workflow import/export](workflow-import-export.md). Its row and column
assertions may only reference columns declared in the Table Schema — a rule the
step-editor form enforces but import bypasses — so
`TabularStepSerializer.validate_imported_ruleset` re-applies the check on import,
raising `vaf.tabular_unknown_column` for an undeclared reference. Every other
inline validator uses the generic base serializer unchanged.
