<?xml version="1.0" encoding="UTF-8"?>
<!--
  Validibot test fixture — a NEUTRAL (non-invoice) Schematron pack.

  The existing fixtures all speak Peppol/UBL invoices. This one deliberately
  models a different, self-contained domain — a simple purchase order — so the
  engine-behaviour tests exercise Schematron *mechanics* (cross-field
  arithmetic, enumerations, multi-namespace resolution, per-severity findings,
  assert-vs-report) without any invoice semantics leaking in.

  It is hand-authored for Validibot's tests; it is NOT derived from any
  published order or e-procurement standard.

  Query binding is the default (XSLT 1.0) so the pack runs under
  lxml.isoschematron (the layer-A / layer-B fixture engine) with no Saxon
  dependency. Two namespaces are in play on purpose — the order body
  (urn:validibot:test:po) and an audit-metadata sidecar
  (urn:validibot:test:audit) — so the `ns` prefix bindings are actually
  load-bearing.

  Rule ids use the VBPO-* prefix so they can never be confused with any real
  standard's identifiers. Each rule targets a distinct behaviour:

    VBPO-STRUCT-01  (fatal)   an order must carry at least one line
    VBPO-CUR-01     (fatal)   currency must be one of an enumerated set
    VBPO-MATH-01    (fatal)   each line's lineTotal must equal qty * unitPrice
    VBPO-MATH-02    (fatal)   grandTotal must equal the sum of line totals
    VBPO-DESC-01    (warning) each line should carry a human description
    VBPO-LEGACY-01  (warning) REPORT (fires when TRUE) a deprecated audit status
    VBPO-NOTE-01    (info)    an order should carry a free-text note

  The arithmetic rules are the whole point: no grammar (XSD/DTD/RelaxNG) can
  express "grandTotal equals the sum of the per-line totals", but Schematron
  can — exactly the capability gap the SchematronValidator closes.
-->
<schema xmlns="http://purl.oclc.org/dsdl/schematron">
  <title>Validibot purchase-order pack (TEST FIXTURE)</title>

  <ns prefix="po" uri="urn:validibot:test:po"/>
  <ns prefix="au" uri="urn:validibot:test:audit"/>

  <pattern>
    <!-- Order-level structural, enumeration and cross-field arithmetic. -->
    <rule context="/po:order">
      <assert test="po:line" flag="fatal" id="VBPO-STRUCT-01"
        >[VBPO-STRUCT-01] An order shall carry at least one line.</assert>
      <assert test="@currency = 'USD' or @currency = 'EUR' or @currency = 'GBP'"
        flag="fatal" id="VBPO-CUR-01"
        >[VBPO-CUR-01] Order currency <value-of select="@currency"/> is not one
        of the accepted codes (USD, EUR, GBP).</assert>
      <assert test="number(po:grandTotal) = sum(po:line/@lineTotal)"
        flag="fatal" id="VBPO-MATH-02"
        >[VBPO-MATH-02] grandTotal (<value-of select="po:grandTotal"/>) must
        equal the sum of the line totals
        (<value-of select="sum(po:line/@lineTotal)"/>).</assert>
      <assert test="po:note" flag="info" id="VBPO-NOTE-01"
        >[VBPO-NOTE-01] An order should carry a free-text note.</assert>
    </rule>

    <!-- Per-line arithmetic and the soft description warning. -->
    <rule context="/po:order/po:line">
      <assert test="number(@lineTotal) = number(@qty) * number(@unitPrice)"
        flag="fatal" id="VBPO-MATH-01"
        >[VBPO-MATH-01] Line <value-of select="@sku"/> lineTotal
        (<value-of select="@lineTotal"/>) must equal qty * unitPrice
        (<value-of select="number(@qty) * number(@unitPrice)"/>).</assert>
      <assert test="@desc" flag="warning" id="VBPO-DESC-01"
        >[VBPO-DESC-01] Line <value-of select="@sku"/> should carry a
        human-readable description.</assert>
    </rule>

    <!-- A REPORT: it fires when the test is TRUE (the opposite of assert),
         so a deprecated audit status surfaces as an active finding. -->
    <rule context="/po:order/au:audit">
      <report test="@status = 'deprecated'" flag="warning" id="VBPO-LEGACY-01"
        >[VBPO-LEGACY-01] This order uses a deprecated audit status.</report>
    </rule>
  </pattern>
</schema>
