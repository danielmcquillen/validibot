<?xml version="1.0" encoding="UTF-8"?>
<!--
  Validibot test fixture — ILLUSTRATIVE SUBSET, *NOT* the official rule set.

  A tiny, hand-written ISO Schematron used to exercise the SchematronValidator
  in unit tests. The real validation packs (EN 16931 by CEN/TC 434 and Peppol
  BIS Billing 3.0 by OpenPEPPOL) contain hundreds of rules, are authored in
  XSLT 2.0, and are vendored + version-pinned separately (see the ADR). Rule
  IDs here are prefixed "VB-" precisely so they cannot be mistaken for the
  canonical BR-* / PEPPOL-EN16931-* identifiers.

  Query binding is deliberately left at the default (XSLT 1.0) so tests can run
  this fixture via lxml.isoschematron without pulling in a Saxon (XSLT 2.0)
  engine. VB-CO-15 mirrors the intent of EN 16931 BR-CO-15.
-->
<schema xmlns="http://purl.oclc.org/dsdl/schematron">
  <title>Validibot Peppol BIS pre-flight subset (TEST FIXTURE)</title>

  <ns prefix="ubl" uri="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>
  <ns prefix="cbc" uri="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"/>
  <ns prefix="cac" uri="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"/>

  <pattern>
    <rule context="/ubl:Invoice">
      <assert test="cbc:DocumentCurrencyCode" flag="fatal" id="VB-CUR-01"
        >[VB-CUR-01] An Invoice shall carry a document currency code.</assert>
      <assert test="cac:InvoiceLine" flag="fatal" id="VB-LINE-01"
        >[VB-LINE-01] An Invoice shall have at least one invoice line.</assert>
    </rule>

    <rule context="/ubl:Invoice/cac:LegalMonetaryTotal">
      <assert test="number(cbc:TaxInclusiveAmount) = number(cbc:TaxExclusiveAmount) + number(/ubl:Invoice/cac:TaxTotal/cbc:TaxAmount)"
        flag="fatal" id="VB-CO-15"
        >[VB-CO-15] Invoice total with VAT (TaxInclusiveAmount) must equal total without VAT (TaxExclusiveAmount) plus the total VAT amount (TaxTotal/TaxAmount).</assert>
    </rule>

    <rule context="/ubl:Invoice/cac:AccountingSupplierParty/cac:Party/cbc:EndpointID">
      <assert test="@schemeID" flag="warning" id="VB-EAS-01"
        >[VB-EAS-01] Supplier electronic address should carry a scheme identifier (e.g. 0208 for a Belgian enterprise number).</assert>
    </rule>
  </pattern>
</schema>
