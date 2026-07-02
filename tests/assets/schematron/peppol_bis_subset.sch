<?xml version="1.0" encoding="UTF-8"?>
<!--
  Validibot test fixture — ILLUSTRATIVE Peppol-LAYER subset, *NOT* the
  official OpenPEPPOL rule set.

  Companion to en16931_subset.sch (the EN 16931-like layer): together
  they model the two-pack layering of the canonical Peppol pre-flight
  workflow (ADR-2026-07-01 D7) — the EN pack checks core business rules,
  and this pack adds the Peppol-specific requirements on top, exactly as
  OpenPEPPOL's PEPPOL-EN16931-* rules layer over CEN's BR-* rules.

  Rule ids use the VB-PEPPOL-* prefix so they can never be confused with
  canonical PEPPOL-EN16931-* identifiers. Query binding is the default
  (XSLT 1.0) so tests run it via lxml.isoschematron with no Saxon
  dependency.
-->
<schema xmlns="http://purl.oclc.org/dsdl/schematron">
  <title>Validibot Peppol-layer subset (TEST FIXTURE)</title>

  <ns prefix="ubl" uri="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"/>
  <ns prefix="cbc" uri="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"/>
  <ns prefix="cac" uri="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"/>

  <pattern>
    <rule context="/ubl:Invoice">
      <assert test="cbc:ProfileID" flag="fatal" id="VB-PEPPOL-R001"
        >[VB-PEPPOL-R001] An Invoice shall declare a business process
        (ProfileID). Mirrors the intent of PEPPOL-EN16931-R001.</assert>
    </rule>
  </pattern>
</schema>
