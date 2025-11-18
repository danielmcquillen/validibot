# ADR: Add XML support for Custom Validators

## Status
Proposed

## Context
Custom validators currently accept a single data format and are limited to JSON (and YAML parsed into JSON-like dict/list structures). Assertion targets and CEL expressions rely on JSON-style paths (`foo.bar[0].baz`). The platform does not support XML submissions in custom validators because:

- There is no XML → in-memory structure normalization for custom validators.
- The assertion/target UX only supports dotted/array paths, not XPath.
- Engines only enforce transport compatibility (file type) and don’t provide XML parsing helpers for custom validators.

We want to keep the idea of XML support visible and scoped, without implementing it immediately.

## Decision
Plan and document the work needed to support XML in custom validators:

1. **Add XML as an allowed data format for custom validators** (initially hidden/disabled in the UI until parsing + targeting is implemented).
2. **Normalization**: introduce an XML → JSON-like representation or native XPath support.
   - Option A (preferred initially): parse XML into a dict/list structure, preserving attributes and namespaces in a predictable way (document the mapping), so existing JSON-style paths continue to work.
   - Option B: allow XPath targets and extend the assertion target resolver to recognize XPath strings; add UI helper text and validation for XPath syntax.
3. **Assertion target handling**:
   - Extend the assertion creation form to accept XPath when the validator’s data_format is XML.
   - Add backend validation to detect malformed XPath and return a structured error.
   - Teach the assertion resolver to execute XPath against the parsed XML tree (or, if using dict representation, align paths accordingly).
4. **Execution safeguards**:
   - In the CustomValidator engine, reject submissions whose file_type is not XML when data_format is XML.
   - Parse XML payloads safely (avoid external entities), and surface clear errors for malformed XML.
5. **Catalog/slug semantics**:
   - Decide how catalog entry slugs map to XML paths (e.g., XPath expressions or normalized JSON-like keys) and document examples so authors can align assertions with catalog entries.
6. **Docs/UI**:
   - Update the validator authoring UI to surface XML as an option once parsing + targeting are in place.
   - Add helper text/examples for XML targets (XPath or normalized path).
7. **Tests**:
   - Add parser unit tests for XML normalization/XPath evaluation.
   - Add assertion resolution tests for XML targets.
   - Add end-to-end validation run tests for custom validators with XML data_format.

## Consequences
- No immediate functional change; this is a roadmap item.
- When implemented, XML-capable custom validators will increase surface area for path handling; we must document the chosen target syntax and normalization rules.
- UI work is required to conditionally allow XML and to guide authors on path syntax.
