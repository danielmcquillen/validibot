"""SHACL validator package — validates RDF graphs against SHACL shapes.

The SHACLValidator is a generic engine for validating RDF documents
(Turtle, JSON-LD, RDF/XML, N-Triples, N-Quads) against SHACL shape
collections. It powers the ASHRAE 223P + Guideline 36 commissioning
workflow as one configuration; the same engine handles Brick Schema,
Project Haystack 4, IFC-OWL, and project-specific shapes.

See ADR-2026-05-18 ``SHACL Validator for RDF Graph Validation`` for the
architecture and phased implementation plan. This package currently
ships scaffolding only — ``SHACLValidator.validate()`` raises
``NotImplementedError`` until Phase 1 lands the engine MVP.
"""

from validibot.validations.validators.shacl.validator import SHACLValidator

__all__ = ["SHACLValidator"]
