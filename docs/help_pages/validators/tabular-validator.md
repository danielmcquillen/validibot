# Tabular Validator

The Tabular Validator checks tabular data — a table of typed rows — against
rules you define. CSV is the file type it reads today (TSV, Excel, and Parquet
are planned), which is why it's called "Tabular" rather than "CSV". Use it to
gate things like a Darwin Core species export, a building energy meter dump, or
any spreadsheet-style file where each column has an expected type and each row
has to make sense.

---

## What schema does it expect?

Validibot stores column rules as a **[Frictionless Table
Schema](https://datapackage.org/standard/table-schema/)**. You normally edit
that schema through the visual **Expected columns** editor: one card per column,
with controls for its name, type, required/unique status, primary-key
membership, and value constraints.

Table Schema is an open standard, not a Validibot invention — the same way our
JSON Schema validator uses JSON Schema and our XML validator uses XSD. If you
already have a descriptor (many open-data portals and research data packages
ship one), import it and review the resulting column cards. If you don't,
Validibot can create a first draft from a sample file. You only need to work
with the JSON directly when importing or exchanging a descriptor.

A minimal descriptor looks like this:

```json
{
  "fields": [
    { "name": "site_id",   "type": "string",  "constraints": { "required": true } },
    { "name": "depth_m",   "type": "number",  "constraints": { "minimum": 0, "maximum": 11000 } },
    { "name": "recorded",  "type": "date" },
    { "name": "status",    "type": "string",  "constraints": { "enum": ["ok", "flagged"] } }
  ],
  "primaryKey": ["site_id"]
}
```

You don't have to learn the format to get started — see the setup paths
below.

---

## Ways to set up the schema

When you configure a Tabular step you can build the column schema three ways:

1. **Infer it from a sample file (the quick path).** Upload a representative
   CSV and select **Infer columns**. Validibot reads the header, resolves the
   delimiter, and guesses each column's type from its values. Review the
   proposal, then select **Apply proposed schema**. Your current unsaved
   columns are not replaced until you apply it. Inference picks types only, so
   add the ranges, allowed values, and required flags that matter for your
   check.
2. **Import a descriptor.** Paste or upload a Frictionless Table Schema JSON
   descriptor and select **Import schema**. Validibot shows a compatibility
   report and proposal before replacing the current editor. Unsupported
   features are preserved where possible but clearly identified as not
   enforced.
3. **Define columns manually.** Select **Add column** and build the expected
   shape directly. This is often quickest for small, stable formats.

Either way, the file's delimiter and whether it has a header row are set
alongside the schema. UTF-8 is fixed in V1. After saving, use **Download saved
schema** to export the portable descriptor.

---

## Defining an expected column

Each column card has:

- **Column name** and **data type** (`Text`, `Integer`, `Number`, `Boolean`,
  `Date`, or `Date and time`).
- **Required**, which means the column must exist and its cells cannot be empty.
- **Required when another column exists**, which keeps this column optional
  unless the selected companion column appears in the submitted file.
- **Unique values**, which rejects repeated non-empty values.
- **Primary key**. Select this on more than one column to create a composite
  key. Primary-key columns are required automatically.
- **Value constraints**. Number columns can have minimum/maximum values; text
  columns can have length and regular-expression rules; every type can use a
  list of allowed values.
- **Move up / Move down** controls. These change the stored order and are
  keyboard accessible.

For headerless files, keep the cards in file-column order. For files with a
header, Validibot matches the declared names to the header.

---

## Two kinds of rule

A Tabular step checks your data in two complementary ways, and both feed the
same findings list:

- **Column rules (from the schema).** Per-column checks — required, conditional
  requiredness, type, range, length, pattern, allowed values, and uniqueness.
  These run automatically.
- **Row rules (CEL assertions).** Anything that compares *across* columns or is
  conditional — for example "minimum depth must not exceed maximum depth", or
  "a recorded date can't be in the future". These are
  [CEL expressions](/app/help/concepts/cel-expressions/) you add to the step
  using the `row.` prefix (e.g. `row.min_depth <= row.max_depth`). They cover
  the rules a column-by-column schema can't express.

The assertion editor groups Tabular rules by execution stage:

- **Dataset assertions** run once against `i.*` metadata such as row count and
  column names.
- **Row assertions** run against each `row.*` value and aggregate failures.
- **Column assertions** run once after row checks against typed aggregates such
  as `col.depth.null_ratio`, `col.depth.distinct_count`, `col.depth.min`,
  `col.depth.max`, and numeric `col.depth.sum`.

A rule can only reference a column you've declared in the schema, so a typo'd
column name is caught when you save the step, not at run time.

Use the Add button in a specific section when you already know the stage, or
the global **Add assertion** button to choose Dataset, Row, or Column. The CEL
editor suggests the namespaces, declared columns, aggregates, and helper
functions available in that stage. Row assertions also let you choose how many
example row numbers each finding should include; the total failure count is
always retained.

---

## File types

The Tabular Validator reads **CSV**. Make sure your workflow's allowed file
types include CSV so the validator is selectable on the step.

---

## Where to learn more

- **Table Schema standard** — the field types and constraint keywords are
  documented at [datapackage.org](https://datapackage.org/standard/table-schema/).
- **CEL expressions** — see [CEL Expressions](/app/help/concepts/cel-expressions/)
  for writing row rules.
- **Full guide** — the complete walkthrough lives in the
  [User Documentation](https://docs.validibot.com/).
