# Tabular Validator

The Tabular Validator checks tabular data — a table of typed rows — against
rules you define. CSV is the file type it reads today (TSV, Excel, and Parquet
are planned), which is why it's called "Tabular" rather than "CSV". Use it to
gate things like a Darwin Core species export, a building energy meter dump, or
any spreadsheet-style file where each column has an expected type and each row
has to make sense.

---

## What schema does it expect?

Column rules are described with a **[Frictionless Table
Schema](https://datapackage.org/standard/table-schema/)** — a small, plain-JSON
description of your columns: each field's name, its type, and any constraints
(required, a numeric range, a string length, a regex pattern, an allowed set of
values, or uniqueness).

Table Schema is an open standard, not a Validibot invention — the same way our
JSON Schema validator uses JSON Schema and our XML validator uses XSD. If you
already have a descriptor (many open-data portals and research data packages
ship one), you can drop it straight in. If you don't, Validibot can write a
first draft for you from a sample file.

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

You don't have to learn the format to get started — see the two setup paths
below.

---

## Two ways to set up the schema

When you configure a Tabular step you supply the column schema one of two ways:

1. **Infer it from a sample file (the quick path).** Upload a representative
   CSV and Validibot reads the header, works out the delimiter, and guesses each
   column's type from its values. This is a *starting point* you then tighten —
   inference picks types only, so you add the ranges, enums, and "required"
   flags that matter for your check.
2. **Paste or import a descriptor.** If you already have a Frictionless Table
   Schema descriptor, paste it in and the column rules populate directly.

Either way, the file's delimiter and whether it has a header row are set
alongside the schema.

---

## Two kinds of rule

A Tabular step checks your data in two complementary ways, and both feed the
same findings list:

- **Column rules (from the schema).** Per-column checks — required, type, range,
  length, pattern, allowed values, and uniqueness. These come straight from the
  Table Schema descriptor and run automatically.
- **Row rules (CEL assertions).** Anything that compares *across* columns or is
  conditional — for example "minimum depth must not exceed maximum depth", or
  "a recorded date can't be in the future". These are
  [CEL expressions](/app/help/concepts/cel-expressions/) you add to the step
  using the `row.` prefix (e.g. `row.min_depth <= row.max_depth`). They cover
  the rules a column-by-column schema can't express.

A rule can only reference a column you've declared in the schema, so a typo'd
column name is caught when you save the step, not at run time.

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
