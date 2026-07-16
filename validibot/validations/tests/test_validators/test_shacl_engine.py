"""Tests for the SHACL engine helpers that remain on the Django side.

After SHACL became an advanced (container) validator, all RDF parsing, pyshacl
execution, SPARQL evaluation, finding mapping, and output extraction moved into
``validibot-validator-backends`` — and so did their tests (see that repo's
``validator_backends/shacl/tests/``). What remains in ``engine`` are the two
Django-free helpers used to *build* the container input envelope:

- :func:`engine.detect_serialization` — resolve the rdflib format slug shipped to
  the container so it parses deterministically.
- :func:`engine.merge_shapes_and_ontologies` — combine library-default shapes with
  step-level extras before shipping the merged text.

This suite covers exactly those two. The execution/security behaviour they used to
guard is now pinned in the backend repo's engine tests.
"""

from __future__ import annotations

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.validators.shacl import engine

# ════════════════════════════════════════════════════════════════════════════
# detect_serialization
# ════════════════════════════════════════════════════════════════════════════


class TestDetectSerialization:
    """Verify the explicit override > extension > file_type > default chain.

    The resolved slug is what Django ships to the container as
    ``SHACLInputs.rdf_format``; the container parses with it directly (it can't
    see the original filename), so this resolution is the only place format
    detection happens.
    """

    def test_explicit_override_wins(self):
        """When the operator sets submission_format, we honour it.

        Important: extension mismatch shouldn't override the operator's
        explicit choice — they may know better than the filename.
        """
        result = engine.detect_serialization(
            file_name="something.ttl",
            file_type=SubmissionFileType.JSON,
            explicit_format="jsonld",
        )
        assert result == "json-ld"

    def test_auto_falls_through_to_extension(self):
        """``explicit_format='auto'`` triggers extension-based detection."""
        result = engine.detect_serialization(
            file_name="model.ttl",
            file_type=SubmissionFileType.TEXT,
            explicit_format="auto",
        )
        assert result == "turtle"

    def test_extension_jsonld_detected(self):
        """``.jsonld`` extension maps to rdflib's "json-ld"."""
        result = engine.detect_serialization("model.jsonld", None, "auto")
        assert result == "json-ld"

    def test_extension_ntriples_detected(self):
        """``.nt`` extension maps to rdflib's N-Triples parser."""
        result = engine.detect_serialization("building.nt", None, "auto")
        assert result == "nt"

    def test_extension_nquads_detected(self):
        """``.nq`` extension maps to rdflib's N-Quads parser."""
        result = engine.detect_serialization("building.nq", None, "auto")
        assert result == "nquads"

    def test_file_type_xml_falls_back_to_rdf_xml(self):
        """No extension hint + XML file_type → RDF/XML.

        Validibot submissions sometimes come without filename information
        (e.g. CLI uploads), and we still want a defensible guess.
        """
        result = engine.detect_serialization(None, SubmissionFileType.XML, "auto")
        assert result == "xml"

    def test_unknown_falls_back_to_turtle(self):
        """When all format hints are unhelpful, default to Turtle.

        Turtle is the most common SHACL serialization, so it's the right
        "I don't know, take a guess" default.
        """
        result = engine.detect_serialization(None, None, "auto")
        assert result == "turtle"


# ════════════════════════════════════════════════════════════════════════════
# merge_shapes_and_ontologies
# ════════════════════════════════════════════════════════════════════════════


class TestMergeShapesAndOntologies:
    """Verify the library-default + step-extras merge contract.

    This is the seam between system step config (step ruleset only) and library
    custom validator config (default ruleset + step extras), so the merge
    ordering matters: library defaults come first, step layers on top, mirroring
    the assertion-merge pattern in ``BaseValidator.evaluate_assertions_for_stage``.
    """

    def test_only_step_shapes_returns_step_content(self):
        """When there's no library default, step shapes pass through."""
        shapes, ont, bundles = engine.merge_shapes_and_ontologies(
            default_shapes_text="",
            default_ontology_text="",
            default_bundled_standards=None,
            step_shapes_text="step shapes",
            step_ontology_text="step ontology",
            step_bundled_standards=["qudt-2.1"],
        )
        assert shapes == "step shapes"
        assert ont == "step ontology"
        assert bundles == ["qudt-2.1"]

    def test_only_default_shapes_returns_default_content(self):
        """When the step has no extras, the library default flows through."""
        shapes, ont, bundles = engine.merge_shapes_and_ontologies(
            default_shapes_text="library shapes",
            default_ontology_text="library ontology",
            default_bundled_standards=["brick-1.4"],
            step_shapes_text="",
            step_ontology_text="",
            step_bundled_standards=None,
        )
        assert shapes == "library shapes"
        assert ont == "library ontology"
        assert bundles == ["brick-1.4"]

    def test_both_present_default_first_step_second(self):
        """When both contribute, the library default comes first.

        This ordering matters because some SHACL features (like
        ``sh:targetClass`` overrides) resolve last-write-wins; putting the
        project-specific extras after the library means project overrides can
        refine library shapes.
        """
        shapes, _, _ = engine.merge_shapes_and_ontologies(
            default_shapes_text="LIBRARY",
            default_ontology_text="",
            default_bundled_standards=None,
            step_shapes_text="STEP",
            step_ontology_text="",
            step_bundled_standards=None,
        )
        assert shapes.startswith("LIBRARY")
        assert shapes.endswith("STEP")
        assert engine.FILE_SEPARATOR in shapes

    def test_step_bundled_standards_overrides_default(self):
        """Step-level bundled selection wins, even an explicit empty list.

        Operators need a way to opt OUT of a library default's bundled standards
        on a per-step basis (e.g. "this workflow doesn't need QUDT even though
        the library validator includes it").
        """
        _, _, bundles = engine.merge_shapes_and_ontologies(
            default_shapes_text="",
            default_ontology_text="",
            default_bundled_standards=["brick-1.4", "qudt-2.1"],
            step_shapes_text="",
            step_ontology_text="",
            step_bundled_standards=[],  # explicit opt-out
        )
        assert bundles == []

    def test_step_bundled_standards_none_inherits_default(self):
        """``None`` step bundles means inherit; only ``[]`` opts out.

        The None-vs-empty-list distinction is the difference between "I didn't
        say anything" and "I explicitly want zero bundles."
        """
        _, _, bundles = engine.merge_shapes_and_ontologies(
            default_shapes_text="",
            default_ontology_text="",
            default_bundled_standards=["brick-1.4"],
            step_shapes_text="",
            step_ontology_text="",
            step_bundled_standards=None,
        )
        assert bundles == ["brick-1.4"]
