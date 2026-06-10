"""Security regression tests for XML schema external-resource resolution.

WHY THIS SUITE EXISTS
---------------------
``XmlSchemaValidator._load_schema`` compiles author-supplied XSD / RelaxNG /
DTD text. Schema text is just as untrusted as the instance document: a ruleset
author (or anyone who can influence ``validator.config['schema']``) controls it.

A malicious schema can embed ``xs:import`` / ``xs:include`` /
``xi:include`` whose ``schemaLocation`` points at a ``file://`` URL (local file
disclosure / path traversal) or an ``http://`` URL (SSRF — forcing the server to
open an outbound socket to an attacker-chosen host). Historically the schema was
compiled with lxml's BARE default parser, so libxml2 would dereference those
references *during schema compilation* — reading the file or opening the socket.

The fix attaches a hardened ``etree.XMLParser`` (``no_network``,
``resolve_entities=False``) PLUS a custom resolver that REFUSES every external
system URL. The resolver is the load-bearing part: libxml2's schema compiler
resolves ``schemaLocation`` through the parser's resolver chain rather than
honouring ``no_network`` alone for ``file://`` URLs.

These tests pin that behaviour so a future refactor cannot silently reintroduce
local-file disclosure or SSRF through a crafted schema. They call the private
``_load_schema`` helper directly because that is the exact unit that compiles
the untrusted schema — no database, submission, or ruleset machinery is needed
to exercise (or regress) the vulnerability.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from validibot.validations.constants import XMLSchemaType
from validibot.validations.validators.xml_schema.validator import XmlSchemaValidator

# A unique marker we plant inside a local file. If schema compilation ever reads
# the file, this string is the canary we assert must NOT surface anywhere.
SECRET_MARKER = "VALIDIBOT-XSD-SECRET-LEAK-CANARY"  # noqa: S105 - test canary, not a credential


def test_malicious_xsd_file_schemalocation_does_not_disclose_local_file() -> None:
    """A ``file://`` ``schemaLocation`` must not read or leak local file content.

    This is the core threat the fix addresses. We write a real file to disk
    containing a secret marker, then feed ``_load_schema`` an XSD whose
    ``xs:import`` points at that file via ``file://``. With the bare parser the
    old code dereferenced the path and read the file during compilation; with the
    hardened parser + blocking resolver, compilation must fail to resolve the
    import and must never expose the file's contents.

    We assert the secret marker never appears in any raised exception message,
    which would be the disclosure channel for an attacker probing the server's
    filesystem via crafted schema-compilation errors.
    """
    with tempfile.TemporaryDirectory() as tmp:
        secret_path = Path(tmp) / "secret.xsd"
        # The file is itself a syntactically valid imported schema, so the ONLY
        # thing preventing a successful import is our resolver refusing to fetch
        # it — proving the block, not an incidental parse error.
        secret_path.write_text(
            "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
            f"targetNamespace='http://evil.example/ns'><!-- {SECRET_MARKER} -->"
            "<xs:element name='leaked' type='xs:string'/></xs:schema>",
            encoding="utf-8",
        )

        malicious_xsd = (
            "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
            "xmlns:ev='http://evil.example/ns' "
            "targetNamespace='http://main.example/ns'>"
            "<xs:import namespace='http://evil.example/ns' "
            f"schemaLocation='file://{secret_path}'/>"
            "<xs:element name='doc'><xs:complexType><xs:sequence>"
            "<xs:element ref='ev:leaked'/>"
            "</xs:sequence></xs:complexType></xs:element></xs:schema>"
        )

        # Compilation must NOT succeed: the import cannot be resolved, so the
        # referenced element type is unknown and lxml raises rather than reading
        # the file. (If it ever returns a schema, the import resolved == leak.)
        with pytest.raises(Exception) as exc_info:  # noqa: PT011
            XmlSchemaValidator()._load_schema(
                schema_type=XMLSchemaType.XSD.name,
                raw=malicious_xsd,
            )

        # The decisive assertion: the local file's secret content is never
        # disclosed through the error surface.
        assert SECRET_MARKER not in str(exc_info.value)


def test_legitimate_self_contained_xsd_still_compiles_and_validates() -> None:
    """Hardening must not break normal, self-contained schemas.

    The blocking resolver only refuses *external* references; a schema with no
    imports/includes must still compile and validate instances exactly as
    before. This guards against an over-broad fix that would reject all schemas
    (a denial-of-service regression for legitimate users).
    """
    from lxml import etree

    good_xsd = (
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
        "<xs:element name='doc' type='xs:string'/></xs:schema>"
    )

    schema = XmlSchemaValidator()._load_schema(
        schema_type=XMLSchemaType.XSD.name,
        raw=good_xsd,
    )

    # A matching instance validates; a mismatching one does not — proving the
    # compiled schema is real and functional, not a neutered no-op.
    assert schema.validate(etree.fromstring(b"<doc>hello</doc>")) is True
    assert schema.validate(etree.fromstring(b"<other/>")) is False
