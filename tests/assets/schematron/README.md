# Schematron test assets

Fixtures for the `SchematronValidator` (see
`validibot-project/docs/adr/2026-07-01-schematron-validator.md`). They use a
Peppol BIS Billing 3.0 (UBL) invoice as the worked domain example, mirroring the
other domain folders under `tests/assets/` (`xml/`, `xsd/`, `rng/`, `fmu/`,
`idf/`, `json/`).

| File | Purpose |
|------|---------|
| `peppol_billing_subset.sch` | A **tiny, illustrative** ISO Schematron subset — **not** the official EN 16931 / Peppol rule set. XSLT 1.0 query binding so it runs under `lxml.isoschematron` with no Saxon dependency. Rule IDs are `VB-*` to avoid being confused with canonical `BR-*` / `PEPPOL-*` IDs. |
| `peppol_invoice_valid.xml` | Well-formed invoice whose totals reconcile. Expected result: **pass** (0 errors; may emit informational/warning findings only). |
| `peppol_invoice_invalid.xml` | Well-formed, XSD-valid invoice with a **seeded defect**: `TaxInclusiveAmount` (120.00) ≠ `TaxExclusiveAmount` (100.00) + `TaxAmount` (21.00). Expected result: **fail** with one `ERROR` finding carrying rule id `VB-CO-15`. |

## Why these fixtures exist

The invalid invoice is deliberately **structurally valid** — a UBL XSD would
accept it — yet it violates an arithmetic relationship across three separate
elements. A grammar (XSD/DTD/RelaxNG) cannot express that constraint; Schematron
can. The pair demonstrates the exact capability gap the `SchematronValidator`
closes.

> These are hand-built test fixtures, not real invoices and not conformance
> samples endorsed by any authority. Validibot's Schematron support is a
> pre-flight developer aid, not a certification of legal e-invoicing compliance.
