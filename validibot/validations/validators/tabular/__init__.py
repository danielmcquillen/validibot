"""Tabular Validator — an in-process validator for tabular data.

The validator's primitive is "a table of typed rows"; CSV is simply the
V1 *reader* in front of a shared validation core (TSV/Excel/Parquet are
future readers, not separate validators). See ADR-2026-05-26 (Tabular
Validator) for the full design.

This package is built up across implementation slices:

- ``readers/`` + ``preflight`` — parse a submission into an in-memory
  dataframe with enforceable caps and deterministic, locale-free reads.
- (later) native structured validation, row-stage CEL evaluation, the
  ``TabularValidator`` class, and its ``config`` for registration.

Until a ``config.py`` exists here, validator auto-discovery
(``validators/base/config.py::discover_configs``) skips this package, so
the in-progress modules below are importable for tests without the
validator being registered or surfaced as a choice.
"""
