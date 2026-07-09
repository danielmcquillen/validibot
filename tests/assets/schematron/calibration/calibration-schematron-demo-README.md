# Calibration certificate demo (XSD + Schematron)

A small, DCC-inspired calibration certificate used as the worked example in the
Validibot blog post on pairing an **XSD structural check** with a **Schematron
business-rule check** (and issuing a validation credential on pass).

This is a deliberately tiny **teaching profile** — *not* the official Digital
Calibration Certificate format, and not a compliance certification. It exists to
show the capability gap between "is the document shaped correctly?" (XSD) and
"do the document's claims agree with each other?" (Schematron).

## Files

| File | Purpose |
|------|---------|
| `calibration-certificate-demo.xsd` | The document-shape contract: required elements/attributes, decimal types, and the allowed `pass` / `fail` / `review` verdicts. |
| `calibration-certificate-valid.xml` | A coherent certificate. Passes **both** the XSD and every Schematron rule. |
| `calibration-certificate-invalid.xml` | Still shaped like a certificate — it is **XSD-valid** — but violates cross-field rules a grammar cannot express (see below). |
| `calibration-rules-demo.sch` | The Schematron business rules (`queryBinding="xslt2"`; ids are `CAL-*`). |
| `calibration-validation-credential-example.json` | Illustrative shape of the validation credential issued on a passing run. |

## Why the "invalid" file is still XSD-valid

That is the whole point. The invalid certificate has the wrong issue date, a
missing accreditation id, a `psi` result unit against an `MPa` instrument, a
point outside the instrument range, and pass verdicts that contradict the
tolerance maths. None of those are things an XSD can see — so the structural
layer accepts the file and the Schematron layer is what catches it. A grammar
proves *shape*; Schematron proves *meaning*.

## Where these files are exercised

These are not just download samples for the blog — they are live test fixtures:

- **XSD layer** — `validibot/validations/tests/test_validators/test_calibration_xsd.py`
  drives the real `XmlSchemaValidator` and asserts that **both** XML files are
  structurally valid against the XSD (the premise the two-layer demo rests on).
- **Schematron layer** — because the rules are XSLT 2.0, they can only run under
  the real Saxon engine, so their execution is covered in the
  `validibot-validator-backends` repo at
  `validator_backends/schematron/tests/test_calibration_engine.py` (a copy of
  the `.sch` + the two XML files lives beside that test, since that suite must
  be self-contained).
