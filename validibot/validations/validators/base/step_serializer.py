"""Per-validator (de)serialization of a workflow step's validator body.

A workflow ``.vaf`` archive captures the whole workflow graph, but the part that
is *validator-specific* — the ruleset (rules + metadata + assertions) attached to
a step — is owned here, by a serializer keyed to the validator. This is what the
import/export design means by "each validator knows how to serialize and
deserialize its own description": the generic exporter/importer walks the
Workflow → Step graph and hands each step's ruleset body to the step's
serializer.

The :class:`StepSerializer` base already round-trips the common shape every
inline validator uses — ``rules_text`` (or a bundled ``rules_file``),
``metadata``, and the ordered list of assertions. A validator subclasses it only
when it needs special handling on the way *in*; the Tabular Validator, for
example, overrides :meth:`StepSerializer.validate_imported_ruleset` to re-run the
"row assertions may only reference declared columns" check so a malformed import
fails loudly instead of creating a broken ruleset.

Resolution: a validator opts into a custom serializer by setting
``step_serializer_class`` on its :class:`ValidatorConfig`. :func:`get_step_serializer`
resolves that dotted path lazily (caching the instance), falling back to the base
serializer when none is declared.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from hashlib import sha256
from typing import TYPE_CHECKING
from typing import Any

from django.utils.module_loading import import_string

if TYPE_CHECKING:
    from validibot.users.models import Organization
    from validibot.users.models import User
    from validibot.validations.models import Ruleset
    from validibot.validations.models import RulesetAssertion
    from validibot.validations.models import StepIODefinition

# A resolver the importer passes in so an assertion that targets a io_definition can be
# re-bound to the freshly created/looked-up StepIODefinition. It maps a serialized
# io_definition reference (contract_key, direction, owner) to a live row, or None.
IODefinitionResolver = Callable[[dict[str, Any]], "StepIODefinition | None"]


class WorkflowImportError(Exception):
    """A definition that is structurally valid JSON but cannot be imported.

    Carries a ``vaf.*`` ``code`` so the import view can render a precise reason.
    Raised by serializers when validator-specific invariants fail (e.g. a
    tabular row assertion references a column the Table Schema doesn't declare).
    """

    def __init__(self, message: str, *, code: str = "vaf.import_failed") -> None:
        super().__init__(message)
        self.code = code


# The assertion fields that round-trip verbatim. Kept as one list so export and
# import can't drift on which fields are carried. ``target_io_definition`` is
# handled separately (it's an FK that must be re-bound), as is ``ruleset``.
_ASSERTION_SCALAR_FIELDS = (
    "order",
    "assertion_type",
    "operator",
    "target_data_path",
    "severity",
    "when_expression",
    "message_template",
    "success_message",
    "notes",
    "cel_cache",
    "spec_version",
)
_ASSERTION_JSON_FIELDS = ("rhs", "options")


class StepSerializer:
    """Serialize/deserialize a step's ruleset body. Generic, subclass to extend.

    Stateless; one shared instance per validator is fine (resolved and cached by
    :func:`get_step_serializer`).
    """

    # ─────────────────────────────────────────────────────────── export ──

    def export_ruleset(
        self,
        ruleset: Ruleset | None,
        *,
        files: dict[str, bytes],
    ) -> dict[str, Any] | None:
        """Serialize a step's ruleset (and its assertions) to a plain dict.

        Returns ``None`` when the step has no ruleset. A ruleset stores its rules
        either inline in ``rules_text`` (pasted schemas, the Table Schema) **or**
        in an uploaded ``rules_file`` (JSON/XML schema uploads, which clear
        ``rules_text``). When the rules live in a file, the bytes are bundled into
        *files* by content hash and referenced — otherwise an imported ruleset
        would have neither text nor file and fail model validation.
        """
        if ruleset is None:
            return None
        body: dict[str, Any] = {
            "name": ruleset.name,
            "ruleset_type": ruleset.ruleset_type,
            "version": ruleset.version or "",
            "metadata": deepcopy(ruleset.metadata) or {},
            "assertions": [
                self._export_assertion(assertion)
                for assertion in ruleset.assertions.all().order_by("order", "pk")
            ],
        }
        if ruleset.rules_file:
            with ruleset.rules_file.open("rb") as handle:
                payload = handle.read()
            file_hash = sha256(payload).hexdigest()
            files[file_hash] = payload
            body["rules_text"] = ""
            body["rules_file"] = {
                "filename": ruleset.rules_file.name.rsplit("/", 1)[-1],
                "content_ref": file_hash,
            }
        else:
            body["rules_text"] = ruleset.rules_text or ""
        return body

    def _export_assertion(self, assertion: RulesetAssertion) -> dict[str, Any]:
        """Serialize one assertion, including a re-bindable io_definition reference."""
        data: dict[str, Any] = {
            field: getattr(assertion, field) for field in _ASSERTION_SCALAR_FIELDS
        }
        for field in _ASSERTION_JSON_FIELDS:
            data[field] = deepcopy(getattr(assertion, field)) or {}
        io_definition = assertion.target_io_definition
        data["target_io_definition_ref"] = (
            _export_io_definition_ref(io_definition) if io_definition else None
        )
        return data

    # ─────────────────────────────────────────────────────────── import ──

    def create_ruleset_row(
        self,
        body: dict[str, Any],
        *,
        org: Organization,
        user: User,
        files: dict[str, bytes],
    ) -> Ruleset:
        """Create the Ruleset row (no assertions) for the importing org.

        Mirrors ``WorkflowVersioningService._clone_ruleset``: a fresh row owned by
        the importing org/user, ``full_clean()``-validated before save. When the
        rules were exported as a bundled file (uploaded JSON/XML schemas), the
        bytes are restored from *files*; otherwise the inline ``rules_text`` is
        used. Assertions are added later (:meth:`create_assertions`) because they
        may target step-owned step I/O definitions that don't exist until their step is
        created — the same create-order the cloner uses.
        """
        from validibot.validations.models import Ruleset

        ruleset_type = body["ruleset_type"]
        version = str(body.get("version") or "") or None
        ruleset = Ruleset(
            org=org,
            user=user,
            # Rulesets are unique by (org, ruleset_type, name, version), so a
            # re-import into the same org needs a fresh name — mirrors the
            # cloner's _unique_ruleset_clone_name.
            name=_unique_ruleset_name(
                org=org,
                ruleset_type=ruleset_type,
                base_name=str(body.get("name") or "Imported ruleset"),
                version=version,
            ),
            ruleset_type=ruleset_type,
            version=version,
            metadata=deepcopy(body.get("metadata") or {}),
        )
        self._apply_rules_content(ruleset, body, files)
        ruleset.full_clean()
        ruleset.save()
        return ruleset

    @staticmethod
    def _apply_rules_content(
        ruleset: Ruleset,
        body: dict[str, Any],
        files: dict[str, bytes],
    ) -> None:
        """Set the ruleset's rules from a bundled file or inline text.

        A ``rules_file`` reference requires its bytes to be present in *files*
        (i.e. a ``.vaf`` import, not bare JSON) — otherwise the schema would be
        lost, so the import fails with a clear, actionable error.
        """
        from django.core.files.base import ContentFile

        rules_file_ref = body.get("rules_file")
        if rules_file_ref:
            content_ref = rules_file_ref.get("content_ref")
            payload = files.get(content_ref) if content_ref else None
            if payload is None:
                raise WorkflowImportError(
                    "This workflow's schema is stored as an uploaded file; "
                    "import the .vaf archive, not the .json.",
                    code="vaf.missing_bundled_file",
                )
            ruleset.rules_text = ""
            ruleset.rules_file = ContentFile(
                payload,
                name=rules_file_ref.get("filename") or "schema",
            )
        else:
            ruleset.rules_text = body.get("rules_text") or ""

    def create_assertions(
        self,
        ruleset: Ruleset,
        body: dict[str, Any],
        *,
        io_definition_resolver: IODefinitionResolver,
    ) -> int:
        """Create the ruleset's assertions in order; return how many were made.

        An I/O-definition-targeted assertion is re-bound via
        *io_definition_resolver*. Call after the step and its I/O definitions exist.
        """
        count = 0
        for assertion_data in body.get("assertions") or []:
            self._build_assertion(ruleset, assertion_data, io_definition_resolver)
            count += 1
        return count

    def build_ruleset(
        self,
        body: dict[str, Any],
        *,
        org: Organization,
        user: User,
        io_definition_resolver: IODefinitionResolver,
        files: dict[str, bytes] | None = None,
    ) -> Ruleset:
        """Create a ruleset and assertions without step-owned I/O definitions.

        Convenience for the common case (and tests) where assertions don't target
        step-owned step I/O definitions. The importer uses the split methods above
        when it must interleave step and I/O-definition creation.
        """
        ruleset = self.create_ruleset_row(body, org=org, user=user, files=files or {})
        self.create_assertions(
            ruleset,
            body,
            io_definition_resolver=io_definition_resolver,
        )
        self.validate_imported_ruleset(ruleset, body)
        return ruleset

    def _build_assertion(
        self,
        ruleset: Ruleset,
        data: dict[str, Any],
        io_definition_resolver: IODefinitionResolver,
    ) -> RulesetAssertion:
        """Create one assertion row and rebind its step I/O definition."""
        from validibot.validations.models import RulesetAssertion

        # Skip fields the definition omits so the model's own default applies.
        # A definition exported before a scalar field existed (e.g. ``notes``)
        # simply has no key for it; passing the resulting ``None`` through would
        # defeat the model default and hit the column's NOT NULL constraint,
        # because Django's clean_fields skips ``blank=True`` empty values rather
        # than coercing them. This mirrors how the importer rebinds other rows
        # (``_create_workflow``, ``_import_input_bindings``): present-and-set
        # wins, absent falls back to the default. Keeps adding an optional
        # assertion field backward-compatible without bumping ``format_version``.
        kwargs: dict[str, Any] = {
            field: data[field]
            for field in _ASSERTION_SCALAR_FIELDS
            if data.get(field) is not None
        }
        for field in _ASSERTION_JSON_FIELDS:
            kwargs[field] = deepcopy(data.get(field) or {})

        io_definition_ref = data.get("target_io_definition_ref")
        kwargs["target_io_definition"] = (
            io_definition_resolver(io_definition_ref) if io_definition_ref else None
        )
        assertion = RulesetAssertion(ruleset=ruleset, **kwargs)
        assertion.full_clean()
        assertion.save()
        return assertion

    def validate_imported_ruleset(
        self,
        ruleset: Ruleset,
        body: dict[str, Any],
    ) -> None:
        """Validator-specific post-import validation hook. Base: no-op.

        Subclasses raise :class:`WorkflowImportError` when an invariant the
        model layer doesn't enforce is violated (e.g. tabular row assertions
        referencing undeclared columns — a check that normally lives in the
        step-editor form, which import bypasses).
        """


# ─────────────────────────────────────────────────────────── helpers ──


def _unique_ruleset_name(
    *,
    org: Organization,
    ruleset_type: str,
    base_name: str,
    version: str | None,
) -> str:
    """Return a ruleset name unique within ``(org, ruleset_type, name, version)``.

    Suffixes `` (2)``, `` (3)``, … on collision so a workflow can be imported
    repeatedly into the same org without tripping the ruleset's natural key.
    """
    from validibot.validations.models import Ruleset

    base = base_name[:200]

    def taken(name: str) -> bool:
        return Ruleset.objects.filter(
            org=org,
            ruleset_type=ruleset_type,
            name=name,
            version=version,
        ).exists()

    if not taken(base):
        return base
    suffix = 2
    while True:
        candidate = f"{base[:190]} ({suffix})"
        if not taken(candidate):
            return candidate
        suffix += 1


def _export_io_definition_ref(io_definition: StepIODefinition) -> dict[str, Any]:
    """Serialize a re-bindable reference to a StepIODefinition.

    Records the stable ``(contract_key, direction)`` plus the owner kind so the
    importer can resolve a validator-owned io_definition via the resolved validator, or
    a step-owned io_definition within the freshly imported step.
    """
    return {
        "contract_key": io_definition.contract_key,
        "direction": io_definition.direction,
        "owner": "validator" if io_definition.validator_id else "step",
    }


# ───────────────────────────────────────────────────── registry ──

_SERIALIZER_CACHE: dict[str, StepSerializer] = {}
_BASE_SERIALIZER = StepSerializer()


def get_step_serializer(validation_type: str) -> StepSerializer:
    """Return the StepSerializer for a validation type (cached).

    Resolves ``ValidatorConfig.step_serializer_class`` lazily; falls back to the
    shared base serializer when the validator declares none or isn't registered.
    """
    from validibot.validations.validators.base.config import get_config

    if validation_type in _SERIALIZER_CACHE:
        return _SERIALIZER_CACHE[validation_type]

    config = get_config(validation_type)
    serializer: StepSerializer = _BASE_SERIALIZER
    if config is not None and config.step_serializer_class:
        serializer = import_string(config.step_serializer_class)()
    _SERIALIZER_CACHE[validation_type] = serializer
    return serializer
