"""Shared SHACL form pieces: multi-file widget, config mixin, constants.

Lives here (under the validator's own package) so both the workflow
step config form (``validibot.workflows.forms.ShaclStepConfigForm``)
and the library-validator create/update forms
(``validibot.validations.forms.ShaclLibraryValidator*Form``) can
declare the same SHACL configuration UI without duplication or a
cross-app import.

The mixin pattern works with Django forms because field declarations
are picked up via the metaclass at class-creation time — fields
declared on a mixin become part of the concrete form's
``base_fields``. Order of inheritance: mixin first, then base form.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django import forms
from django.conf import settings as django_settings
from django.forms.forms import DeclarativeFieldsMetaclass
from django.utils.translation import gettext_lazy as _

from validibot.validations.constants import Severity
from validibot.validations.validators.shacl.sparql_security import SparqlScrubError
from validibot.validations.validators.shacl.sparql_security import scrub_sparql_ask

logger = logging.getLogger(__name__)

# Map of lower-cased aliases → canonical ``Severity`` enum value the
# engine and persistence layer use. Accepts both upper-case
# (``"ERROR"``) and lower-case (``"error"``) author input; normalises
# to the canonical enum string before saving so downstream comparisons
# never have to handle both forms.
_SEVERITY_ALIASES: dict[str, str] = {
    "error": Severity.ERROR,
    "warning": Severity.WARNING,
    "warn": Severity.WARNING,
    "info": Severity.INFO,
}
_VALID_TARGET_GRAPHS: frozenset[str] = frozenset({"data", "results", "union"})


# Per-file cap matches the existing schema-upload limit. 10 MB aggregate
# allows a typical 223P + G36 + project shapes bundle.
SHACL_PER_FILE_MAX_BYTES = 2 * 1024 * 1024
SHACL_TOTAL_UPLOAD_MAX_BYTES = 10 * 1024 * 1024


SHACL_INFERENCE_CHOICES = (
    ("none", _("None (fastest; skips subclass reasoning)")),
    ("rdfs", _("RDFS (recommended for 223P, Brick, Haystack)")),
    ("owlrl", _("OWL 2 RL (most thorough, slowest)")),
)


SHACL_SUBMISSION_FORMAT_CHOICES = (
    ("auto", _("Auto-detect from file extension")),
    ("turtle", _("Turtle (.ttl)")),
    ("jsonld", _("JSON-LD (.jsonld)")),
    ("rdfxml", _("RDF/XML (.rdf)")),
    ("nt", _("N-Triples (.nt)")),
    ("nquads", _("N-Quads (.nq)")),
)


# SHACL shape/ontology persistence concatenates saved content into one
# ``Ruleset.rules_text`` blob that the engine parses as Turtle. Submission RDF
# may still be JSON-LD/RDF-XML/N-Triples/N-Quads; this cap applies only to
# uploaded SHACL configuration files.
_SHACL_EXT_FORMAT: dict[str, str] = {
    "ttl": "turtle",
}


def _max_asks_per_step() -> int:
    """Read the per-step SPARQL ASK cap from Django settings.

    Defaults to 25 to match ``engine.DEFAULT_SPARQL_ASKS_PER_STEP``.
    Operators tighten this via ``SHACL_SPARQL_ASKS_PER_STEP_MAX``.
    """
    raw = getattr(django_settings, "SHACL_SPARQL_ASKS_PER_STEP_MAX", 25)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 25
    return value if value > 0 else 25


def _normalise_sparql_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Coerce a validated assertion dict into the persistence shape.

    All fields are stringified; severity is normalised through
    :data:`_SEVERITY_ALIASES` to the canonical ``Severity`` enum value
    (upper-case "ERROR" / "WARNING" / "INFO") so downstream code never
    needs to handle case variations. Missing optional fields default to
    empty strings. Called only after :meth:`_validate_sparql_entry`
    accepts the entry.
    """
    severity_raw = str(entry.get("severity", Severity.ERROR)).strip().lower()
    severity = _SEVERITY_ALIASES.get(severity_raw, Severity.ERROR)
    return {
        "target_graph": str(entry.get("target_graph", "data")),
        "query": str(entry["query"]).strip(),
        "severity": severity,
        "description": str(entry.get("description", "") or ""),
        "error_message_template": str(
            entry.get("error_message_template", "") or "",
        ),
    }


class _MultipleFileInput(forms.ClearableFileInput):
    """File input widget that accepts more than one file at once.

    Django's stock ``FileInput`` rejects multiple files because the
    underlying HTML ``<input type="file">`` element only emits one
    upload by default. Setting ``allow_multiple_selected = True`` plus
    rendering ``multiple`` on the HTML element gives bulk-upload
    semantics so an author can drop a 223P shapes file alongside a
    project-specific shapes file in one step.
    """

    allow_multiple_selected = True

    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        attrs = dict(attrs or {})
        attrs.setdefault("multiple", "multiple")
        super().__init__(attrs)


class _MultipleFileField(forms.FileField):
    """File field that returns a list of files instead of a single file.

    Pairs with :class:`_MultipleFileInput`. ``cleaned_data`` carries
    every uploaded file rather than just the last one.
    """

    widget = _MultipleFileInput

    def clean(self, data: Any, initial: Any = None) -> list[Any]:
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_file_clean(d, initial) for d in data if d]
        if data in self.empty_values:
            return []
        return [single_file_clean(data, initial)]


class ShaclConfigMixin(metaclass=DeclarativeFieldsMetaclass):
    """Form mixin contributing the SHACL configuration fields + clean helpers.

    Applies ``DeclarativeFieldsMetaclass`` directly so the field
    declarations below populate ``declared_fields`` and are collected by
    Django's form metaclass when this mixin is combined with a concrete
    ``forms.Form`` subclass. Without this, the metaclass only scans
    bases that already have ``declared_fields`` (i.e. other forms).

    Used by both the workflow step config form and the library-validator
    create/update forms. The shared fields cover:

    - Multi-file upload for shapes (``shapes_files``) + inline text
      fallback (``shapes_text``).
    - Multi-file upload for ontologies (``ontology_files``) + inline
      text fallback (``ontology_text``).
    - Bundled-standards checkboxes (``bundle_brick``, ``bundle_qudt``);
      Phase 1 emits a warning when checked because the bundle content
      ships in Phase 2.
    - Engine knobs (``inference_mode``, ``advanced_shacl``,
      ``submission_format``).

    The mixin also provides :meth:`shacl_clean_uploads` which runs an
    rdflib parse pass on every uploaded file. Consumer forms call this
    from their own ``clean()`` after deciding the "shapes are required"
    rule for their context (step config requires shapes; library
    validator create requires them too; library validator update has
    keep-existing semantics).
    """

    shapes_files = _MultipleFileField(
        label=_("SHACL shape files"),
        required=False,
        help_text=_(
            "Upload one or more SHACL Turtle shape files (.ttl). "
            "Constraints from every uploaded file are merged into a "
            "single shapes graph and evaluated against the submitted "
            "RDF graph.",
        ),
    )
    shapes_text = forms.CharField(
        label=_("Or paste shapes inline"),
        widget=forms.Textarea(attrs={"rows": 8, "spellcheck": "false"}),
        required=False,
        help_text=_(
            "Optional inline shapes Turtle. Concatenated with the "
            "uploaded files above. Useful for small project-specific "
            "rules where uploading a file would be overkill.",
        ),
    )
    ontology_files = _MultipleFileField(
        label=_("Supplementary ontology files"),
        required=False,
        help_text=_(
            "Upload Turtle ontology files (.ttl) to give the reasoner context "
            "for subclass and property inference. If your shapes file "
            "is also an ontology (true for ASHRAE 223P, where every "
            "class is also a sh:NodeShape), you can leave this empty.",
        ),
    )
    ontology_text = forms.CharField(
        label=_("Or paste ontology inline"),
        widget=forms.Textarea(attrs={"rows": 6, "spellcheck": "false"}),
        required=False,
    )
    # bundle_brick / bundle_qudt fields removed pending Phase 2 (the
    # bundled shapes content ships then). The engine's
    # ``bundled_standards`` plumbing is still in place — the builder
    # services read ``cleaned.get("bundle_brick")``, which is None when
    # the field isn't declared, so the resulting list is always empty
    # and the engine sees no bundle requests. Re-add the BooleanField
    # declarations + the layout entries in the library form when the
    # bundles ship.
    inference_mode = forms.ChoiceField(
        label=_("Inference mode"),
        choices=SHACL_INFERENCE_CHOICES,
        initial="rdfs",
        widget=forms.RadioSelect,
        required=True,
    )
    advanced_shacl = forms.BooleanField(
        label=_("Enable advanced SHACL (SPARQL constraints, SHACL Rules)"),
        required=False,
        initial=True,
        help_text=_(
            "Required when using ASHRAE 223P (its shapes use "
            "sh:SPARQLConstraint for medium-compatibility checks). Safe "
            "to leave on; tiny performance cost.",
        ),
    )
    submission_format = forms.ChoiceField(
        label=_("Submission RDF format"),
        choices=SHACL_SUBMISSION_FORMAT_CHOICES,
        initial="auto",
        required=False,
    )

    # SPARQL ASK assertions. Authors paste a JSON list; each entry is one
    # assertion. Validated at clean time by :meth:`shacl_clean_sparql_assertions`
    # which:
    #   1. Parses the JSON.
    #   2. Validates per-entry shape (target_graph, query, severity).
    #   3. Runs the SPARQL AST scrubber on each query.
    #   4. Caps the total count at ``SHACL_SPARQL_ASKS_PER_STEP_MAX``.
    # The cleaned value is a Python list of dicts ready to persist to
    # ``Ruleset.metadata["sparql_assertions"]``. See ADR-2026-05-18
    # "Phase 1c — SPARQL ASK assertions".
    sparql_assertions_json = forms.CharField(
        label=_("SPARQL ASK assertions (JSON)"),
        widget=forms.Textarea(
            attrs={
                "rows": 10,
                "spellcheck": "false",
                "placeholder": (
                    "[\n"
                    "  {\n"
                    '    "target_graph": "data",\n'
                    '    "query": "ASK { ?s a <http://example.com/Thing> }",\n'
                    '    "severity": "error",\n'
                    '    "description": "Must contain at least one Thing",\n'
                    '    "error_message_template": "No Thing instances found."\n'
                    "  }\n"
                    "]"
                ),
            },
        ),
        required=False,
        help_text=_(
            "Optional list of project-specific SPARQL ASK gates layered "
            "on top of SHACL conformance. Each ASK runs against the "
            "data, results, or union graph; a false answer raises a "
            "finding at the chosen severity. Only SPARQL ASK is "
            "supported in this release — SELECT, CONSTRUCT, DESCRIBE, "
            "and all Update operations are rejected at save time, "
            "as are SERVICE clauses and remote FROM references. "
            "See the SHACL validator docs for example queries.",
        ),
    )

    # ------------------------------------------------------------------
    # Helpers consumers call from their own clean()
    # ------------------------------------------------------------------

    def shacl_enforce_size_caps(self, files: list[Any], field_name: str) -> None:
        """Surface form errors when any file or the aggregate is too big.

        Consumer forms call this from ``clean()`` after extracting
        files from cleaned_data, so the size-cap policy lives in one
        place even though the rules-required policy varies per form.
        """
        total = 0
        for f in files:
            size = getattr(f, "size", 0) or 0
            total += size
            if size > SHACL_PER_FILE_MAX_BYTES:
                self.add_error(
                    field_name,
                    _("%(name)s is %(size)d bytes, over the %(cap)d byte limit.")
                    % {
                        "name": getattr(f, "name", "<unknown>"),
                        "size": size,
                        "cap": SHACL_PER_FILE_MAX_BYTES,
                    },
                )
        if total > SHACL_TOTAL_UPLOAD_MAX_BYTES:
            self.add_error(
                field_name,
                _(
                    "Total upload is %(total)d bytes, over the "
                    "%(cap)d byte aggregate limit.",
                )
                % {"total": total, "cap": SHACL_TOTAL_UPLOAD_MAX_BYTES},
            )

    def shacl_syntax_pre_flight_files(
        self,
        files: list[Any],
        field_name: str,
    ) -> None:
        """Run rdflib parse on each uploaded file; surface errors inline.

        Lazy-imports rdflib so the workflows + validations form modules
        don't pay for it at import time when nothing is using SHACL.
        """
        from rdflib import Graph
        from rdflib.exceptions import ParserError

        for f in files:
            name = getattr(f, "name", "<unknown>")
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            rdf_format = _SHACL_EXT_FORMAT.get(ext)
            if rdf_format is None:
                self.add_error(
                    field_name,
                    _(
                        "%(name)s must be a Turtle .ttl file. Submission RDF "
                        "can still use JSON-LD, RDF/XML, N-Triples, or N-Quads.",
                    )
                    % {"name": name},
                )
                continue
            try:
                f.seek(0)
                content = f.read()
                f.seek(0)
            except Exception as exc:
                # Reading an upload can fail for many reasons (closed
                # stream, network truncation in chunked uploads). Skip
                # rather than aborting clean(); the missing parse check
                # is the worst-case downside.
                logger.warning("SHACL upload %s could not be read: %s", name, exc)
                continue
            if isinstance(content, bytes):
                try:
                    content = content.decode("utf-8")
                except UnicodeDecodeError:
                    self.add_error(
                        field_name,
                        _("%(name)s is not valid UTF-8.") % {"name": name},
                    )
                    continue
            try:
                Graph().parse(data=content, format=rdf_format)
            except ParserError as exc:
                self.add_error(
                    field_name,
                    _("%(name)s failed to parse as %(fmt)s: %(err)s")
                    % {"name": name, "fmt": rdf_format, "err": exc},
                )
            except Exception as exc:
                self.add_error(
                    field_name,
                    _("%(name)s could not be parsed: %(err)s")
                    % {"name": name, "err": exc},
                )

    def shacl_syntax_pre_flight_text(
        self,
        text: str,
        field_name: str,
        rdf_format: str = "turtle",
    ) -> None:
        """Run rdflib parse on inline-pasted text; surface errors inline."""
        from rdflib import Graph
        from rdflib.exceptions import ParserError

        try:
            Graph().parse(data=text, format=rdf_format)
        except ParserError as exc:
            self.add_error(
                field_name,
                _("Inline text failed to parse as %(fmt)s: %(err)s")
                % {"fmt": rdf_format, "err": exc},
            )
        except Exception as exc:
            self.add_error(
                field_name,
                _("Inline text could not be parsed: %(err)s") % {"err": exc},
            )

    def shacl_clean_sparql_assertions(
        self,
        raw_json: str,
        field_name: str = "sparql_assertions_json",
    ) -> list[dict[str, Any]]:
        """Validate and normalise the SPARQL ASK assertions JSON textarea.

        Returns a list of dicts ready to persist into
        ``Ruleset.metadata["sparql_assertions"]``. Adds form errors and
        returns ``[]`` when the input is malformed, the per-entry shape
        is wrong, the count exceeds the per-step cap, or any query fails
        the SPARQL AST scrub.

        Accepts both lower-case (``"error"``) and upper-case
        (``"ERROR"``) severity values; normalises to ``Severity`` enum
        strings before returning.

        Empty / whitespace-only input is treated as "no assertions" and
        returns ``[]`` cleanly.
        """
        text = (raw_json or "").strip()
        if not text:
            return []

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            self.add_error(
                field_name,
                _("Could not parse JSON: %(err)s") % {"err": exc},
            )
            return []

        if not isinstance(data, list):
            self.add_error(
                field_name,
                _(
                    "Top-level JSON value must be a list of assertion "
                    "objects (got %(kind)s).",
                )
                % {"kind": type(data).__name__},
            )
            return []

        cap = _max_asks_per_step()
        if len(data) > cap:
            self.add_error(
                field_name,
                _(
                    "Too many SPARQL ASK assertions: %(count)d entries, "
                    "limit is %(cap)d. Combine or remove some.",
                )
                % {"count": len(data), "cap": cap},
            )
            return []

        cleaned: list[dict[str, Any]] = []
        for index, entry in enumerate(data):
            error = self._validate_sparql_entry(entry, index, field_name)
            if error is None:
                cleaned.append(_normalise_sparql_entry(entry))

        return cleaned

    def _validate_sparql_entry(
        self,
        entry: Any,
        index: int,
        field_name: str,
    ) -> str | None:
        """Run all per-entry checks; add form errors and return summary."""
        prefix = f"Assertion #{index + 1}: "

        if not isinstance(entry, dict):
            self.add_error(
                field_name,
                _("%(p)sentry must be a JSON object, got %(t)s.")
                % {"p": prefix, "t": type(entry).__name__},
            )
            return "not-an-object"

        target = entry.get("target_graph", "data")
        if not isinstance(target, str) or target not in _VALID_TARGET_GRAPHS:
            self.add_error(
                field_name,
                _(
                    "%(p)starget_graph must be one of "
                    "'data' / 'results' / 'union' (got %(v)s).",
                )
                % {"p": prefix, "v": target},
            )
            return "bad-target"

        query = entry.get("query", "")
        if not isinstance(query, str) or not query.strip():
            self.add_error(
                field_name,
                _("%(p)squery is required and must be a non-empty SPARQL ASK.")
                % {"p": prefix},
            )
            return "no-query"

        severity_raw = str(entry.get("severity", Severity.ERROR)).strip().lower()
        if severity_raw not in _SEVERITY_ALIASES:
            self.add_error(
                field_name,
                _(
                    "%(p)sseverity must be one of "
                    "'error' / 'warning' / 'info' (got %(v)s).",
                )
                % {"p": prefix, "v": severity_raw},
            )
            return "bad-severity"

        # Description and error_message_template are optional strings;
        # tolerate non-string values by coercing in _normalise_sparql_entry.

        # Final, most expensive check: run the AST scrubber.
        try:
            scrub_sparql_ask(query)
        except SparqlScrubError as exc:
            self.add_error(
                field_name,
                _("%(p)squery failed security scrub: %(err)s")
                % {"p": prefix, "err": exc},
            )
            return "scrub-rejected"

        return None
