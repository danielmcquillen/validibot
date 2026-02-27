"""Tests for the XML-to-dict conversion utility."""

from __future__ import annotations

import pytest
from django.test import SimpleTestCase

from validibot.validations.xml_utils import XmlParseError
from validibot.validations.xml_utils import xml_to_dict


class XmlToDictSimpleTests(SimpleTestCase):
    """Basic XML-to-dict conversion."""

    def test_simple_flat_elements(self):
        """Flat sibling elements become dict keys."""
        xml = "<root><name>Alice</name><age>30</age></root>"
        result = xml_to_dict(xml)
        self.assertEqual(result, {"root": {"name": "Alice", "age": "30"}})

    def test_nested_children(self):
        """Nested elements become nested dicts."""
        xml = "<root><person><name>Bob</name><city>NYC</city></person></root>"
        result = xml_to_dict(xml)
        self.assertEqual(
            result,
            {"root": {"person": {"name": "Bob", "city": "NYC"}}},
        )

    def test_repeated_elements_become_list(self):
        """Repeated sibling elements are collected into a list."""
        xml = "<root><item>a</item><item>b</item><item>c</item></root>"
        result = xml_to_dict(xml)
        self.assertEqual(result, {"root": {"item": ["a", "b", "c"]}})

    def test_two_repeated_elements_become_list(self):
        """Even two repeated elements create a list."""
        xml = "<root><item>x</item><item>y</item></root>"
        result = xml_to_dict(xml)
        self.assertEqual(result, {"root": {"item": ["x", "y"]}})

    def test_single_element_stays_scalar(self):
        """A single child element stays as a scalar, not a list."""
        xml = "<root><item>only</item></root>"
        result = xml_to_dict(xml)
        self.assertEqual(result, {"root": {"item": "only"}})

    def test_empty_element(self):
        """An element with no text and no children becomes an empty string."""
        xml = "<root><empty/></root>"
        result = xml_to_dict(xml)
        self.assertEqual(result, {"root": {"empty": ""}})


class XmlToDictAttributeTests(SimpleTestCase):
    """XML attributes are prefixed with @."""

    def test_attributes_prefixed(self):
        """Element attributes appear as @-prefixed keys."""
        xml = '<root><item id="42" type="widget">Hello</item></root>'
        result = xml_to_dict(xml)
        self.assertEqual(
            result,
            {"root": {"item": {"@id": "42", "@type": "widget", "#text": "Hello"}}},
        )

    def test_attributes_only(self):
        """Element with only attributes, no text."""
        xml = '<root><config debug="true" verbose="false"/></root>'
        result = xml_to_dict(xml)
        self.assertEqual(
            result,
            {"root": {"config": {"@debug": "true", "@verbose": "false"}}},
        )


class XmlToDictNamespaceTests(SimpleTestCase):
    """Namespace stripping."""

    def test_namespace_stripping_enabled(self):
        """Namespaced tags are stripped by default."""
        xml = '<ns:root xmlns:ns="http://example.com"><ns:item>val</ns:item></ns:root>'
        result = xml_to_dict(xml, strip_namespaces=True)
        self.assertEqual(result, {"root": {"item": "val"}})

    def test_namespace_stripping_disabled(self):
        """With strip_namespaces=False, full namespace URIs are preserved."""
        xml = '<ns:root xmlns:ns="http://example.com"><ns:item>val</ns:item></ns:root>'
        result = xml_to_dict(xml, strip_namespaces=False)
        # lxml/defusedxml expands ns: prefix to {URI}tag
        self.assertIn("{http://example.com}root", result)


class XmlToDictMixedContentTests(SimpleTestCase):
    """Mixed text and child elements."""

    def test_mixed_text_and_children(self):
        """Element with both text and children stores text in #text."""
        xml = "<root>Hello<child>world</child></root>"
        result = xml_to_dict(xml)
        self.assertEqual(
            result,
            {"root": {"#text": "Hello", "child": "world"}},
        )


class XmlToDictErrorHandlingTests(SimpleTestCase):
    """Error handling and safety limits."""

    def test_empty_content_raises(self):
        """Empty string raises XmlParseError."""
        with pytest.raises(XmlParseError, match="Empty XML"):
            xml_to_dict("")

    def test_empty_bytes_raises(self):
        """Empty bytes raises XmlParseError."""
        with pytest.raises(XmlParseError, match="Empty XML"):
            xml_to_dict(b"")

    def test_malformed_xml_raises(self):
        """Malformed XML raises XmlParseError with details."""
        with pytest.raises(XmlParseError, match="Invalid XML"):
            xml_to_dict("<root><unclosed>")

    def test_oversized_xml_rejected(self):
        """Payload exceeding max_size_bytes is rejected before parsing."""
        small_limit = 100
        big_xml = "<root>" + "<item>x</item>" * 20 + "</root>"
        self.assertGreater(len(big_xml.encode()), small_limit)

        with pytest.raises(XmlParseError, match="exceeds maximum size"):
            xml_to_dict(big_xml, max_size_bytes=small_limit)

    def test_xxe_blocked(self):
        """defusedxml blocks XXE entity expansion attacks."""
        xxe_xml = (
            '<?xml version="1.0"?>'
            "<!DOCTYPE foo ["
            '  <!ENTITY xxe SYSTEM "file:///etc/passwd">'
            "]>"
            "<root>&xxe;</root>"
        )
        with pytest.raises(XmlParseError):
            xml_to_dict(xxe_xml)

    def test_billion_laughs_blocked(self):
        """defusedxml blocks entity expansion bomb (billion laughs)."""
        bomb_xml = (
            '<?xml version="1.0"?>'
            "<!DOCTYPE lolz ["
            '  <!ENTITY lol "lol">'
            '  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            "]>"
            "<root>&lol2;</root>"
        )
        with pytest.raises(XmlParseError):
            xml_to_dict(bomb_xml)


class XmlToDictMaliciousInputTests(SimpleTestCase):
    """Tests for inputs a malicious actor might submit."""

    def test_xss_in_element_text_is_plain_string(self):
        """Script tags in element text become plain strings, not executable."""
        xml = '<root><name>&lt;script&gt;alert("xss")&lt;/script&gt;</name></root>'
        result = xml_to_dict(xml)
        # The value is a plain string -- no HTML interpretation
        self.assertEqual(result["root"]["name"], '<script>alert("xss")</script>')

    def test_xss_in_attribute_is_plain_string(self):
        """Script injection in attributes becomes a plain string."""
        xml = (
            "<root>"
            '<item onclick="alert(1)" '
            'data-x="&lt;img onerror=alert(1)&gt;">ok</item>'
            "</root>"
        )
        result = xml_to_dict(xml)
        item = result["root"]["item"]
        self.assertEqual(item["@onclick"], "alert(1)")
        self.assertEqual(item["@data-x"], "<img onerror=alert(1)>")
        self.assertEqual(item["#text"], "ok")

    def test_javascript_uri_in_attribute(self):
        """javascript: URIs in attributes are stored as plain strings."""
        xml = '<root><link href="javascript:alert(1)">click</link></root>'
        result = xml_to_dict(xml)
        self.assertEqual(result["root"]["link"]["@href"], "javascript:alert(1)")

    def test_cdata_with_script(self):
        """CDATA sections with script content become plain text."""
        xml = "<root><code><![CDATA[<script>alert('xss')</script>]]></code></root>"
        result = xml_to_dict(xml)
        self.assertEqual(result["root"]["code"], "<script>alert('xss')</script>")

    def test_external_dtd_blocked(self):
        """External DTD references are blocked by defusedxml."""
        xml = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE root SYSTEM "http://evil.example.com/payload.dtd">'
            "<root>data</root>"
        )
        with pytest.raises(XmlParseError):
            xml_to_dict(xml)

    def test_ssrf_via_entity_blocked(self):
        """SSRF attempts via external entity are blocked."""
        xml = (
            '<?xml version="1.0"?>'
            "<!DOCTYPE foo ["
            '  <!ENTITY ssrf SYSTEM "http://169.254.169.254/latest/meta-data/">'
            "]>"
            "<root>&ssrf;</root>"
        )
        with pytest.raises(XmlParseError):
            xml_to_dict(xml)

    def test_deeply_nested_xml_within_limit(self):
        """XML within max_depth parses successfully."""
        depth = 50
        opening = "".join(f"<n{i}>" for i in range(depth))
        closing = "".join(f"</n{i}>" for i in reversed(range(depth)))
        xml = f"{opening}leaf{closing}"
        result = xml_to_dict(xml, max_depth=depth + 10)
        self.assertIsInstance(result, dict)

    def test_deeply_nested_xml_exceeding_limit_raises(self):
        """XML exceeding max_depth raises XmlParseError, not RecursionError."""
        depth = 50
        opening = "".join(f"<n{i}>" for i in range(depth))
        closing = "".join(f"</n{i}>" for i in reversed(range(depth)))
        xml = f"{opening}leaf{closing}"
        with pytest.raises(XmlParseError, match="nesting depth exceeds maximum"):
            xml_to_dict(xml, max_depth=10)

    def test_deeply_nested_xml_at_exact_limit_succeeds(self):
        """XML at exactly max_depth succeeds (boundary test)."""
        depth = 20
        opening = "".join(f"<n{i}>" for i in range(depth))
        closing = "".join(f"</n{i}>" for i in reversed(range(depth)))
        xml = f"{opening}leaf{closing}"
        # depth=20 elements, max_depth=20 should succeed (root is depth 1)
        result = xml_to_dict(xml, max_depth=depth)
        self.assertIsInstance(result, dict)

    def test_default_max_depth_handles_normal_documents(self):
        """Default max_depth (200) is generous enough for real documents."""
        depth = 100
        opening = "".join(f"<n{i}>" for i in range(depth))
        closing = "".join(f"</n{i}>" for i in reversed(range(depth)))
        xml = f"{opening}leaf{closing}"
        result = xml_to_dict(xml)
        self.assertIsInstance(result, dict)

    def test_null_bytes_in_content(self):
        """Null bytes in XML content are rejected as malformed."""
        xml = "<root><item>hello\x00world</item></root>"
        with pytest.raises(XmlParseError):
            xml_to_dict(xml)


class XmlToDictBytesInputTests(SimpleTestCase):
    """Bytes input handling."""

    def test_bytes_input(self):
        """Bytes input is handled identically to string input."""
        xml_bytes = b"<root><item>hello</item></root>"
        result = xml_to_dict(xml_bytes)
        self.assertEqual(result, {"root": {"item": "hello"}})

    def test_utf8_content(self):
        """UTF-8 encoded content with non-ASCII characters."""
        xml = "<root><name>Muller</name></root>"
        result = xml_to_dict(xml)
        self.assertEqual(result, {"root": {"name": "Muller"}})


class XmlToDictPathResolutionTests(SimpleTestCase):
    """Verify the dict structure works with _resolve_path style dot/bracket paths."""

    def test_dict_supports_dot_path_navigation(self):
        """The resulting dict supports standard dot-path navigation."""
        xml = (
            "<building>"
            "  <thermostat>"
            "    <setpoint>72</setpoint>"
            "    <mode>cool</mode>"
            "  </thermostat>"
            "</building>"
        )
        result = xml_to_dict(xml)
        # Navigate: building.thermostat.setpoint
        self.assertEqual(
            result["building"]["thermostat"]["setpoint"],
            "72",
        )

    def test_dict_supports_list_index_navigation(self):
        """Repeated elements become lists, supporting index-based access."""
        xml = (
            "<building>"
            "  <zone><name>Zone A</name></zone>"
            "  <zone><name>Zone B</name></zone>"
            "</building>"
        )
        result = xml_to_dict(xml)
        zones = result["building"]["zone"]
        self.assertIsInstance(zones, list)
        self.assertEqual(zones[0]["name"], "Zone A")
        self.assertEqual(zones[1]["name"], "Zone B")
