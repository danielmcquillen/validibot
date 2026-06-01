"""Tests for the ``.vaf`` packaging layer (pack / read_input).

The archive format is the transport for workflow import/export, so its contract
matters on its own: a packed definition must read back identically, a bare
``workflow.json`` must be accepted (the file-free path), and a hostile or
malformed archive must be rejected with a precise ``vaf.*`` code rather than
crashing the import view. These are pure functions (no Django), so the suite uses
``SimpleTestCase``.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from django.test import SimpleTestCase

from validibot.workflows.services.io import vaf


def _zip_with(members: dict[str, bytes]) -> bytes:
    """Build a ZIP from a name->bytes map (for crafting hostile archives)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class VafRoundTripTests(SimpleTestCase):
    """Pack then read must reproduce the definition and its bundled files."""

    def test_pack_then_read_round_trips_definition_and_files(self):
        """A packed definition + file reads back byte-identical.

        This is the core guarantee the importer relies on: what export packs,
        import can read — including bundled binary files keyed by content hash.
        """
        definition = {"format_version": 1, "workflow": {"name": "X"}, "steps": []}
        payload = b"\x00\x01binary-bytes"
        files = {vaf.content_hash(payload): payload}

        archive = vaf.pack(definition, files=files)
        bundle = vaf.read_input(archive, filename="w.vaf")

        assert bundle.had_archive is True
        assert bundle.workflow == definition
        assert bundle.files == files

    def test_pack_is_deterministic(self):
        """The same input packs to identical bytes (stable committed artifacts).

        The example ``.vaf`` is committed to the repo; a non-deterministic packer
        would churn it on every regeneration. Sorted members + sorted JSON keys
        keep it stable.
        """
        definition = {"b": 2, "a": 1}
        assert vaf.pack(definition) == vaf.pack(definition)


class VafBareJsonTests(SimpleTestCase):
    """A bare workflow.json is a valid, file-free import input."""

    def test_bare_json_is_accepted_without_an_archive(self):
        """Plain JSON bytes parse as a file-free bundle.

        The ".json works until you need files" rule: a definition with no bundled
        resources can be imported as raw JSON, so ``had_archive`` is False and
        ``files`` is empty.
        """
        definition = {"format_version": 1, "workflow": {"name": "X"}, "steps": []}
        bundle = vaf.read_input(json.dumps(definition).encode(), filename="w.json")

        assert bundle.had_archive is False
        assert bundle.files == {}
        assert bundle.workflow == definition

    def test_non_json_non_zip_is_a_clear_error(self):
        """Garbage bytes raise a precise error, not a stack trace."""
        with pytest.raises(vaf.VafError) as ctx:
            vaf.read_input(b"this is not json or a zip", filename="bad.json")
        assert ctx.value.code == "vaf.invalid_json"

    def test_empty_input_is_rejected(self):
        """An empty upload is rejected before any parsing."""
        with pytest.raises(vaf.VafError) as ctx:
            vaf.read_input(b"", filename="empty.vaf")
        assert ctx.value.code == "vaf.empty"


class VafHostileArchiveTests(SimpleTestCase):
    """Malformed or tampered archives are rejected defensively."""

    def test_archive_without_workflow_json_is_rejected(self):
        """An archive missing workflow.json can't be imported."""
        archive = _zip_with({"manifest.json": b"{}"})
        with pytest.raises(vaf.VafError) as ctx:
            vaf.read_input(archive, filename="w.vaf")
        assert ctx.value.code == "vaf.missing_workflow"

    def test_tampered_file_hash_is_rejected(self):
        """A bundled file whose bytes don't match its name-hash is rejected.

        The file member name *is* the content hash; verifying it on read stops a
        tampered archive from smuggling mismatched bytes past the importer.
        """
        archive = _zip_with(
            {
                "workflow.json": b'{"workflow": {}, "steps": []}',
                # Claims to be the hash of "real" but holds different bytes.
                f"files/{vaf.content_hash(b'real')}": b"tampered",
            },
        )
        with pytest.raises(vaf.VafError) as ctx:
            vaf.read_input(archive, filename="w.vaf")
        assert ctx.value.code == "vaf.hash_mismatch"

    def test_path_traversal_member_is_rejected(self):
        """An archive entry escaping the archive root is rejected."""
        archive = _zip_with(
            {
                "workflow.json": b'{"workflow": {}, "steps": []}',
                "../evil.txt": b"nope",
            },
        )
        with pytest.raises(vaf.VafError) as ctx:
            vaf.read_input(archive, filename="w.vaf")
        assert ctx.value.code == "vaf.unsafe_path"

    def test_unsupported_manifest_version_is_rejected(self):
        """An archive declaring a future format version is refused, not guessed."""
        archive = _zip_with(
            {
                "manifest.json": json.dumps({"vaf_version": 999}).encode(),
                "workflow.json": b'{"workflow": {}, "steps": []}',
            },
        )
        with pytest.raises(vaf.VafError) as ctx:
            vaf.read_input(archive, filename="w.vaf")
        assert ctx.value.code == "vaf.unsupported_version"
