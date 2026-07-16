"""Pure SHACL helpers retained on the Django side.

As of the move to the isolated SHACL container backend, **all RDF parsing,
pyshacl execution, and SPARQL evaluation live in
``validibot-validator-backends/validator_backends/shacl/``** — never in the
Django worker. What remains here are the two Django-free, graph-free helpers
needed to *prepare* the container input envelope (and one constant the workflow
form reuses):

- :func:`detect_serialization` — pick an rdflib format slug from submission
  metadata, so the container is told exactly how to parse.
- :func:`merge_shapes_and_ontologies` — combine library-validator default shapes
  with step-level extras before shipping the merged text in the envelope.

These are pure string/dict operations with no rdflib, no subprocess, and no
Django-settings dependency. They are consumed by
:mod:`validibot.validations.validators.shacl.launch` (envelope construction) and
``workflows.views_helpers`` (which imports :data:`FILE_SEPARATOR`).

The historical engine — parse/run/SPARQL/output-value extraction and the killable
subprocess workers — was removed when SHACL became an advanced (container)
validator. See ADR-2026-05-18 and the cross-repo isolation plan; the engine code
now lives, security checks and all, behind the container boundary.
"""

from __future__ import annotations

from validibot.submissions.constants import SubmissionFileType

# Separator placed between concatenated files inside ``Ruleset.rules`` and
# ``metadata["ontology_text"]``. The workflow form writes the same marker, so the
# value is a shared constant (its exact form is not part of any contract).
FILE_SEPARATOR = "\n# === File boundary ===\n"

# Map filename extensions / format slugs to rdflib format strings. rdflib accepts
# "turtle", "json-ld", "xml" (RDF/XML), "nt" (N-Triples), "nquads", "n3".
_EXTENSION_TO_FORMAT: dict[str, str] = {
    "ttl": "turtle",
    "turtle": "turtle",
    "n3": "n3",
    "jsonld": "json-ld",
    "json-ld": "json-ld",
    "rdf": "xml",
    "rdfxml": "xml",
    "xml": "xml",
    "nt": "nt",
    "ntriples": "nt",
    "nq": "nquads",
    "nquads": "nquads",
}


def detect_serialization(
    file_name: str | None,
    file_type: str | None,
    explicit_format: str | None,
) -> str:
    """Pick an rdflib format string from submission metadata.

    Priority: explicit format > file extension > submission file_type >
    default ("turtle"). The resolved slug is shipped in the container input
    envelope as ``SHACLInputs.rdf_format`` so the container parses deterministically
    (it never has to re-detect from a filename it can't see).

    Args:
        file_name: Submission filename, used to read the extension.
        file_type: ``SubmissionFileType`` value (JSON / XML / TEXT / ...).
        explicit_format: Operator-supplied override from the step config.
            Pass ``None`` or "auto" to enable auto-detection.

    Returns:
        rdflib format string ("turtle", "json-ld", "xml", "nt", "nquads").
    """
    if explicit_format and explicit_format != "auto":
        return _EXTENSION_TO_FORMAT.get(explicit_format.lower(), "turtle")

    if file_name and "." in file_name:
        ext = file_name.rsplit(".", 1)[-1].lower()
        if ext in _EXTENSION_TO_FORMAT:
            return _EXTENSION_TO_FORMAT[ext]

    # Fall back to the broad SubmissionFileType signal. JSON is overwhelmingly
    # JSON-LD in the RDF world; XML is RDF/XML. Everything else defaults to
    # Turtle (the most common serialization).
    if file_type == SubmissionFileType.JSON:
        return "json-ld"
    if file_type == SubmissionFileType.XML:
        return "xml"

    return "turtle"


def merge_shapes_and_ontologies(
    default_shapes_text: str,
    default_ontology_text: str,
    default_bundled_standards: list[str] | None,
    step_shapes_text: str,
    step_ontology_text: str,
    step_bundled_standards: list[str] | None,
) -> tuple[str, str, list[str]]:
    """Merge library-validator defaults with step-level extras.

    Library defaults come first, step-level extras layer on top — mirroring the
    assertion-merge pattern in ``BaseValidator.evaluate_assertions_for_stage``.

    For ``bundled_standards``, a non-None step-level list wins (even an empty list
    signals an intentional opt-out); otherwise the library default is inherited.
    The resolved merged text + bundle list are shipped to the container; the
    container loads the bundle content (Phase 1 stubs it).

    Returns:
        ``(merged_shapes_text, merged_ontology_text, bundled_standards)``.
    """
    shapes_parts: list[str] = []
    if default_shapes_text:
        shapes_parts.append(default_shapes_text)
    if step_shapes_text:
        shapes_parts.append(step_shapes_text)
    shapes_text = FILE_SEPARATOR.join(shapes_parts)

    ontology_parts: list[str] = []
    if default_ontology_text:
        ontology_parts.append(default_ontology_text)
    if step_ontology_text:
        ontology_parts.append(step_ontology_text)
    ontology_text = FILE_SEPARATOR.join(ontology_parts)

    if step_bundled_standards is not None:
        bundled = step_bundled_standards
    else:
        bundled = default_bundled_standards or []

    return shapes_text, ontology_text, bundled
