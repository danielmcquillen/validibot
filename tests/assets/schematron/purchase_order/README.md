# Purchase-order Schematron test assets (neutral domain)

A **non-invoice** fixture pack for the `SchematronValidator` engine-behaviour
tests. The other assets under `tests/assets/schematron/` all speak Peppol/UBL
invoices; this pack deliberately models a different, self-contained domain — a
simple purchase order in two namespaces — so tests can exercise Schematron
*mechanics* (cross-field arithmetic, enumerations, multi-namespace resolution,
per-severity findings, `assert` vs `report`) without invoice semantics leaking
in.

These are hand-authored for Validibot's tests. They are **not** derived from any
published purchase-order or e-procurement standard, and rule ids use the
`VBPO-*` prefix so they can never be mistaken for a real standard's ids.

The pack is XSLT-1.0 query binding on purpose, so it runs under
`lxml.isoschematron` (the layer-A / layer-B fixture engine) with no Saxon
dependency.

## Namespaces

| Prefix | URI | Role |
|--------|-----|------|
| `po`   | `urn:validibot:test:po`    | the order body (order, line, grandTotal, note) |
| `au`   | `urn:validibot:test:audit` | an audit-metadata sidecar (audit/@status)      |

Two namespaces are in play so the `ns` prefix bindings in the `.sch` are
actually load-bearing — a binding mistake would make a rule context match
nothing.

## Rules (`purchase_order.sch`)

| Rule id | Severity | Kind | Checks |
|---------|----------|------|--------|
| `VBPO-STRUCT-01` | fatal   | assert | an order has at least one line |
| `VBPO-CUR-01`    | fatal   | assert | currency is one of USD / EUR / GBP |
| `VBPO-MATH-01`   | fatal   | assert | each line's `lineTotal` = `qty` * `unitPrice` |
| `VBPO-MATH-02`   | fatal   | assert | `grandTotal` = sum of the line totals |
| `VBPO-DESC-01`   | warning | assert | each line carries a `desc` |
| `VBPO-LEGACY-01` | warning | **report** | fires when `audit/@status` = `deprecated` |
| `VBPO-NOTE-01`   | info    | assert | the order carries a `note` |

`VBPO-MATH-02` is the load-bearing example: no grammar (XSD/DTD/RelaxNG) can
express "grandTotal equals the sum of the per-line totals", but Schematron can.

## Fixtures and expected outcomes

| File | error | warning | info | `passed` |
|------|:-----:|:-------:|:----:|:--------:|
| `purchase_order_valid.xml` | 0 | 0 | 0 | true |
| `purchase_order_warnings_only.xml` | 0 | 2 (`VBPO-LEGACY-01`, `VBPO-DESC-01`) | 1 (`VBPO-NOTE-01`) | **true** |
| `purchase_order_bad_math.xml` | 1 (`VBPO-MATH-02`) | 1 (`VBPO-DESC-01`) | 0 | false |

All three MATCH every rule context (order, each line, audit), so
`fired_rule_count` is 4 in each case — a run over the valid document still
proves the rules genuinely evaluated rather than silently matching nothing.

`purchase_order_warnings_only.xml` is the **"passes with warnings"** case
(ADR-2026-07-01 D3): a Schematron run passes iff there are zero ERROR-level
findings, so non-fatal findings surface without failing the run.
