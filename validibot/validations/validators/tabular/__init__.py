"""Tabular Validator — an in-process validator for tabular data.

The validator's primitive is "a table of typed rows"; CSV is simply the
V1 *reader* in front of a shared validation core (TSV/Excel/Parquet are
future readers, not separate validators). See ADR-2026-05-26 (Tabular
Validator) for the full design.

The package is organised as a pipeline:

- ``readers/`` + ``preflight`` — parse a submission into an in-memory
  dataframe with enforceable caps and deterministic, locale-free reads.
- ``schema`` + ``coercion`` — the internal Table Schema model and its
  locale-free cell coercion.
- ``native`` — structured validation (required/type/range/length/pattern/
  enum/uniqueness) against the schema.
- ``row_eval`` — per-row CEL assertions (the ``row.*`` namespace) with a
  compiled-once-per-run loop and a pinned ``now()``.
- ``infer`` — derive a starter schema from a sample.
- ``validator`` + ``config`` — the registered ``TabularValidator`` and the
  ``ValidatorConfig`` that auto-discovery (``validators/base/config.py::
  discover_configs``) reads to surface it as a choice.
"""
