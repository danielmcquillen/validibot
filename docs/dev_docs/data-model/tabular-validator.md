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
    `TabularValidator`, the **step settings editor** (dialect + paste/infer a
    schema), and a step-detail **summary card** are all in place. After applying
    migrations and running `manage.py sync_validators`, an author can select
    **Tabular Validator**, configure it on the step settings page, add `row.*`
    assertions, and run it. A `row.<column>` reference to a column not declared
    in the step's schema is rejected at save time (the ADR's column-existence
    obligation). Remaining polish is a richer settings UX — inline HTMx
    column-constraint CRUD instead of the paste/infer textarea.

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

There are two ways to populate that descriptor: paste/import an existing one, or
**infer one from a sample file** (the fastest common path — most users have a
CSV, not a hand-written descriptor). Inference (`infer.py`) reads a bounded
sample, resolves the dialect and column names through the normal reader, and
guesses each column's type from its values using the *same* coercion the
validator enforces (so an inferred type means what validation will check).
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
`report_max_examples` sample row numbers — never one finding per failing row, so
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

A run does five things: load the schema (a schema that won't parse is a
`tabular.invalid_schema` finding, not a crash); read the body (a read failure
becomes a finding carrying its `tabular.*` code); run native validation, mapping
each `NativeFinding` onto a platform `ValidationIssue` with the count and sample
rows preserved in `meta`; run the **row-stage CEL** loop (below); and run the
standard dataset (`i.*`) / output CEL assertion lane. The `i.*` dataset signals
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
`tabular_stage="row"`, anything else (`i.*`/`s.*`) tags it `"dataset"`. The
`col.*` namespace is deferred with V2 column assertions, so it is rejected for
now even on a tabular step — an author can't save a rule the engine can't run.
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

## Configuring a step (the settings editor)

A tabular step is configured on the normal full-screen step settings page
(`steps/<id>/settings/`), which renders `TabularStepConfigForm` — the same
per-validator config-form mechanism JSON Schema and SHACL use. The form has the
dialect fields (delimiter / header) and two ways to supply the column schema:
**paste a Frictionless descriptor**, or **upload a sample CSV to infer one** (via
`infer.py`). Encoding is pinned to UTF-8 in V1 (not an editable field):
submitted content reaches the validator already decoded as UTF-8, so a per-step
encoding setting could only silently corrupt non-UTF-8 input — honoring other
encodings needs a raw-bytes read path, a future slice. On save,
`build_tabular_config` writes the descriptor to `ruleset.rules_text` and the
dialect to `ruleset.metadata` — the exact places the validator reads back at run
time, so the editor and the engine meet at the ruleset.

When editing an existing step the schema textarea starts **empty**: leaving it
blank keeps the stored schema (only the dialect is updated), pasting a new
descriptor replaces it, and the current schema is shown read-only beneath the
field. This avoids round-tripping a truncated preview back as a replacement,
which would corrupt a schema larger than the preview cap.

The step-detail page shows a read-only **summary card** (reader, delimiter,
header, column count, rule count) with an "Edit settings" link. Row/dataset CEL
assertions are authored through the existing step-assertion surface (the form
accepts `row.*` on a tabular step and tags the stage — see above).

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
| `validations/validators/tabular/validator.py` | `TabularValidator`: reads the submission, runs native + row-stage validation, maps findings to `ValidationIssue`, runs the dataset/output CEL lane |
| `validations/validators/tabular/config.py` | The `ValidatorConfig` that makes the validator discoverable and DB-syncable |
| `workflows/forms.py` (`TabularStepConfigForm`) | Step settings form: dialect fields + paste/infer the schema |
| `workflows/views_helpers.py` (`build_tabular_config`) | Writes the descriptor to `ruleset.rules_text` and dialect to `ruleset.metadata` on save |
| `workflows/step_configs.py` (`TabularStepConfig`) | Typed display config for the step-detail summary card |
| `templates/.../components/tabular_config_card.html` | The read-only step-detail summary card |

Read failures are raised as a `TabularReadError` carrying a machine-readable
`code` (always prefixed `tabular.`, e.g. `tabular.file_too_large`,
`tabular.parse_error`). Native validation returns `NativeFinding`s carrying the
same kind of `tabular.*` code (e.g. `tabular.out_of_range`,
`tabular.unique_violation`) plus a count and sample rows, so the validator can
emit structured findings without matching on message text.
