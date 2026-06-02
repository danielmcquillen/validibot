"""SHACL validator package — validates RDF graphs against SHACL shapes.

``SHACLValidator`` validates RDF documents (Turtle, JSON-LD, RDF/XML,
N-Triples, N-Quads) against SHACL shape collections. It powers the
ASHRAE 223P + Guideline 36 commissioning workflow as one configuration;
the same validator handles Brick Schema, Project Haystack 4, IFC-OWL,
and project-specific shapes.

SHACL is an **advanced (container) validator**: it parses untrusted RDF
and executes author-supplied SPARQL, so all graph/SPARQL execution runs
in the isolated ``validibot-validator-backend-shacl`` container, never in
the worker. This package is the Django-side half — it resolves shapes,
settings, and SPARQL-ASK assertions from the database (:mod:`launch`),
dispatches via :class:`AdvancedValidator` (:mod:`validator`), and scrubs
author SPARQL at form-save time (:mod:`sparql_security`). The execution
engine lives in ``validibot-validator-backends``.

See ADR-2026-05-18 ``SHACL Validator for RDF Graph Validation`` for the
engine design and the cross-repo plan for the isolation rationale.
"""

from validibot.validations.validators.shacl.validator import SHACLValidator

__all__ = ["SHACLValidator"]
