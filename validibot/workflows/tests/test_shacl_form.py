"""Tests for the SHACL step config form and its builder helper.

Covers two surfaces that live in the workflows app:

- :class:`ShaclStepConfigForm` — bulk file upload, inline-text fallback,
  bundled-standards checkboxes, engine-knob fields, plus syntax pre-flight
  that rejects malformed Turtle at edit time rather than at validation time.
- :func:`build_shacl_config` — turns the form's cleaned data into a
  ``Ruleset`` row + a ``WorkflowStep.config`` dict, with file-boundary
  comments preserved in ``rules_text`` and per-file metadata
  (name/size/sha256) on ``Ruleset.metadata``.

The integration that wires these into ``save_workflow_step`` lives in
:func:`validibot.workflows.views_helpers.save_workflow_step` and is
covered indirectly by the orchestrator tests in
:mod:`validibot.validations.tests.test_validators.test_shacl_validator`.
"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from validibot.projects.tests.factories import ProjectFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.shacl.constants import SHACL_RESULT_REPORT_ONLY
from validibot.validations.validators.shacl.form_fields import SHACL_INFERENCE_CHOICES
from validibot.workflows.forms import ShaclStepConfigForm
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.views_helpers import build_shacl_config

# Reusable inline Turtle fixtures kept tiny so the test diffs read well.
VALID_SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.com/> .

ex:PersonShape
    a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:property [ sh:path ex:name ; sh:minCount 1 ] .
"""

VALID_ONTOLOGY = """
@prefix ex: <http://example.com/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

ex:Person a rdfs:Class .
"""

LIBRARY_SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.com/> .

ex:LibraryShape
    a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:property [ sh:path ex:id ; sh:minCount 1 ] .
"""

MALFORMED_TURTLE = "this is not valid turtle <<<"

# Validator/builder contract constants — broken out so ruff PLR2004
# doesn't fire on the magic-number comparisons in the assertions.
SHA256_HEX_LENGTH = 64
SHAPES_PREVIEW_MAX_CHARS = 1200


def _shapes_file(name: str = "shapes.ttl", content: str = VALID_SHAPES):
    """Build a SimpleUploadedFile suitable for the form's multi-file field."""
    return SimpleUploadedFile(name, content.encode("utf-8"), content_type="text/turtle")


class ShaclStepConfigFormCleanTests(TestCase):
    """Verify form validation rules: required shapes, size caps, syntax check.

    The form is the operator's first feedback surface — when something is
    wrong with the upload, errors should appear inline in the step
    editor before the workflow gets saved.
    """

    def test_inline_text_alone_is_valid(self):
        """An inline shapes paste with no file upload is enough to save.

        Mirrors the JSON Schema form's text-or-file pattern: the
        author shouldn't be forced to wrap small shapes in a .ttl file
        upload just to use the validator.
        """
        form = ShaclStepConfigForm(
            data={
                "name": "Step name",
                "description": "",
                "shapes_text": VALID_SHAPES,
                "ontology_text": "",
                "inference_mode": "rdfs",
                "advanced_shacl": True,
                "submission_format": "auto",
            },
        )
        assert form.is_valid(), form.errors

    def test_uploaded_shapes_file_alone_is_valid(self):
        """A single uploaded shapes file is enough to save."""
        form = ShaclStepConfigForm(
            data={
                "name": "Step name",
                "description": "",
                "inference_mode": "rdfs",
                "advanced_shacl": True,
                "submission_format": "auto",
            },
            files={"shapes_files": _shapes_file()},
        )
        assert form.is_valid(), form.errors

    def test_no_shapes_at_all_fails_for_new_step(self):
        """When creating a fresh step, leaving shapes blank should error.

        We can't validate against nothing — an empty shapes blob in
        rules_text would silently pass every submission, which is a
        worse failure mode than rejecting the save.
        """
        form = ShaclStepConfigForm(
            data={
                "name": "Step name",
                "description": "",
                "inference_mode": "rdfs",
                "advanced_shacl": True,
                "submission_format": "auto",
            },
        )
        assert not form.is_valid()
        assert "shapes_files" in form.errors or "shapes_text" in form.errors

    def test_malformed_inline_shapes_fail_with_specific_error(self):
        """Bad Turtle inline → form error mentions the parse failure.

        Syntax pre-flight at save time saves the operator a round trip
        through the actual validation run to discover their Turtle is
        broken.
        """
        form = ShaclStepConfigForm(
            data={
                "name": "Step name",
                "shapes_text": MALFORMED_TURTLE,
                "inference_mode": "rdfs",
                "advanced_shacl": True,
                "submission_format": "auto",
            },
        )
        assert not form.is_valid()
        assert "shapes_text" in form.errors
        # Verify the error mentions parsing so operators know what to fix.
        joined = " ".join(form.errors["shapes_text"]).lower()
        assert "parse" in joined

    def test_malformed_uploaded_shapes_file_fails_with_filename_in_error(self):
        """Bad file content → form error names the offending file.

        Multi-file uploads can include many shapes files; the operator
        needs to know which one is broken without trial-and-error.
        """
        form = ShaclStepConfigForm(
            data={
                "name": "Step name",
                "inference_mode": "rdfs",
                "advanced_shacl": True,
                "submission_format": "auto",
            },
            files={
                "shapes_files": SimpleUploadedFile(
                    "broken-rules.ttl",
                    MALFORMED_TURTLE.encode("utf-8"),
                ),
            },
        )
        assert not form.is_valid()
        joined = " ".join(form.errors["shapes_files"])
        assert "broken-rules.ttl" in joined

    def test_oversized_single_file_rejected(self):
        """A file over the 2 MB per-file cap surfaces a clear error.

        Prevents accidentally uploading a multi-megabyte ontology
        bundle that would slow every validation run.
        """
        from validibot.workflows.forms import SHACL_PER_FILE_MAX_BYTES

        oversized = SimpleUploadedFile(
            "huge.ttl",
            b"# padding\n" + b"x" * (SHACL_PER_FILE_MAX_BYTES + 1),
        )
        form = ShaclStepConfigForm(
            data={
                "name": "Step name",
                "inference_mode": "rdfs",
                "advanced_shacl": True,
                "submission_format": "auto",
            },
            files={"shapes_files": oversized},
        )
        assert not form.is_valid()
        joined = " ".join(form.errors.get("shapes_files", []))
        assert "limit" in joined.lower() or "over" in joined.lower()

    def test_bundled_standards_fields_not_exposed_in_phase_1(self):
        """The Brick + QUDT checkbox fields are hidden until Phase 2 ships.

        The engine's ``bundled_standards`` plumbing is still in place
        (verified by engine + library merge tests) — what's hidden is
        the UI exposure. When the bundles ship, re-add the fields to
        :class:`ShaclConfigMixin` and re-introduce this test as a
        positive assertion.
        """
        form = ShaclStepConfigForm(
            data={
                "name": "Step name",
                "shapes_text": VALID_SHAPES,
                "inference_mode": "rdfs",
                "advanced_shacl": True,
                "submission_format": "auto",
            },
        )
        assert form.is_valid(), form.errors
        # Confirm the form genuinely doesn't expose these fields.
        assert "bundle_brick" not in form.fields
        assert "bundle_qudt" not in form.fields

    def test_existing_step_initializes_when_bundle_fields_are_hidden(self):
        """Editing a saved SHACL step should not reference hidden bundle fields."""

        ruleset = RulesetFactory(
            ruleset_type=RulesetType.SHACL,
            rules_text=VALID_SHAPES,
            metadata={"bundled_standards": []},
        )
        step = WorkflowStepFactory(
            ruleset=ruleset,
            config={
                "bundled_standards": [],
                "shapes_text_preview": VALID_SHAPES[:SHAPES_PREVIEW_MAX_CHARS],
            },
        )

        form = ShaclStepConfigForm(step=step)

        assert "bundle_brick" not in form.fields
        assert "bundle_qudt" not in form.fields

    def test_library_validator_default_shapes_allow_blank_step_shapes(self):
        """Library SHACL validators carry reusable default shapes.

        A workflow author should be able to add that validator without
        re-uploading the shapes into every step; step-level shapes are
        only project-specific extras.
        """

        default_ruleset = RulesetFactory(
            ruleset_type=RulesetType.SHACL,
            rules_text=VALID_SHAPES,
        )
        validator = ValidatorFactory(
            validation_type=ValidationType.SHACL,
            default_ruleset=default_ruleset,
            is_system=False,
            supports_assertions=True,
        )

        form = ShaclStepConfigForm(
            data={
                "name": "Reusable library validator",
                "inference_mode": "rdfs",
                "advanced_shacl": True,
                "submission_format": "auto",
            },
            validator=validator,
        )

        assert form.is_valid(), form.errors

    def test_non_turtle_shape_upload_is_rejected(self):
        """Shape uploads must match the engine's Turtle persistence format.

        Submission RDF can use other serializations, but shape/ontology
        uploads are concatenated into one Turtle blob before the engine
        reads them.
        """

        form = ShaclStepConfigForm(
            data={
                "name": "Step name",
                "inference_mode": "rdfs",
                "advanced_shacl": True,
                "submission_format": "auto",
            },
            files={
                "shapes_files": SimpleUploadedFile(
                    "shapes.jsonld",
                    b'{"@context": {"sh": "http://www.w3.org/ns/shacl#"}}',
                    content_type="application/ld+json",
                ),
            },
        )

        assert not form.is_valid()
        assert "shapes_files" in form.errors
        joined = " ".join(form.errors["shapes_files"]).lower()
        assert "turtle" in joined


class ShaclInferenceOptionFormTests(TestCase):
    """Verify the form exposes exactly the three supported inference modes.

    The engine tests
    (:class:`validibot.validations.tests.test_validators.test_shacl_engine.TestInferenceModes`)
    prove what each mode *does*. These tests prove the operator-facing form
    offers precisely those modes plus the advanced toggle, and rejects
    anything outside that set. Together they close the loop between the UI
    options and the engine: a mode could work in the engine but be
    unreachable from the form, or the form could offer a mode pyshacl would
    crash on — both are caught here.
    """

    def _base_data(self, **overrides):
        """Minimal valid form payload; override one field per test."""
        data = {
            "name": "Step name",
            "description": "",
            "shapes_text": VALID_SHAPES,
            "ontology_text": "",
            "inference_mode": "rdfs",
            "advanced_shacl": False,
            "submission_format": "auto",
        }
        data.update(overrides)
        return data

    def test_choices_match_the_three_engine_supported_modes(self):
        """The form's choice keys are exactly the values pyshacl accepts here.

        The keys ride untranslated into ``pyshacl.validate(inference=...)``,
        so a drifting key (e.g. ``"owl2rl"``) would pass form validation and
        only blow up at run time inside the worker subprocess. Pinning the
        keys catches that at edit time. The labels are asserted too because
        they are the operator's only guidance on which mode to pick — if a
        label lost its "223P" hint or "fastest" cue, the UI's recommendation
        semantics would silently change.
        """
        assert [key for key, _label in SHACL_INFERENCE_CHOICES] == [
            "none",
            "rdfs",
            "owlrl",
        ]
        labels = {key: str(label) for key, label in SHACL_INFERENCE_CHOICES}
        assert "fastest" in labels["none"].lower()
        assert "223P" in labels["rdfs"]
        assert "OWL 2 RL" in labels["owlrl"]

    def test_form_accepts_each_supported_inference_mode(self):
        """Every documented mode validates — none of the three is unreachable.

        If a choice were declared in the dropdown but rejected by ``clean()``
        (e.g. silently dropped from the ChoiceField's choices), an operator
        could see the option yet never be able to save it. Walking all three
        guarantees each is selectable end-to-end.
        """
        for mode in ("none", "rdfs", "owlrl"):
            with self.subTest(inference_mode=mode):
                form = ShaclStepConfigForm(data=self._base_data(inference_mode=mode))
                assert form.is_valid(), form.errors
                assert form.cleaned_data["inference_mode"] == mode

    def test_form_rejects_unsupported_inference_mode(self):
        """A mode outside the three (e.g. pyshacl's ``"both"``) must not validate.

        pyshacl also understands ``"both"`` / ``"all"``, but Validibot
        deliberately does not expose them — the docs, resource budgets, and
        performance guidance only account for the three documented modes.
        This guards against silently widening the surface: if ``"both"`` ever
        became valid here, an operator could pick a mode the rest of the
        product never planned for.
        """
        form = ShaclStepConfigForm(data=self._base_data(inference_mode="both"))
        assert not form.is_valid()
        assert "inference_mode" in form.errors

    def test_advanced_toggle_accepts_on_and_off_and_defaults_off(self):
        """The advanced-SHACL checkbox round-trips both states; default is off.

        ``advanced=False`` is the safe default — advanced SHACL additionally
        requires the deployment-level ``SHACL_ENABLE_ADVANCED_FEATURES`` flag
        — so an omitted (unchecked) checkbox must clean to ``False`` rather
        than erroring. Both explicit states must also validate so an author
        can deliberately turn the feature on per step.
        """
        # Checked.
        on = ShaclStepConfigForm(data=self._base_data(advanced_shacl=True))
        assert on.is_valid(), on.errors
        assert on.cleaned_data["advanced_shacl"] is True

        # Unchecked / omitted: BooleanField(required=False) cleans to False.
        data = self._base_data()
        data.pop("advanced_shacl")
        off = ShaclStepConfigForm(data=data)
        assert off.is_valid(), off.errors
        assert off.cleaned_data["advanced_shacl"] is False


class BuildShaclConfigTests(TestCase):
    """Verify the builder helper that turns form data into Ruleset + config.

    Tests the persistence shape committed to the database — particularly
    that the metadata dict carries everything the engine needs at
    validation time (engine knobs, bundled standards, per-file metadata).
    """

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        cls.project = ProjectFactory(org=cls.org)
        cls.workflow = WorkflowFactory(org=cls.org, user=cls.user)

    def _bound_form(self, **overrides):
        """Shorthand for a valid form binding used by most tests below."""
        data = {
            "name": "Step name",
            "description": "Step description",
            "shapes_text": VALID_SHAPES,
            "ontology_text": "",
            "inference_mode": "rdfs",
            "advanced_shacl": True,
            "submission_format": "auto",
        }
        data.update(overrides)
        form = ShaclStepConfigForm(data=data)
        assert form.is_valid(), form.errors
        return form

    def test_builder_creates_shacl_ruleset_with_shapes(self):
        """build_shacl_config writes a SHACL ruleset with shapes in rules_text."""
        form = self._bound_form()
        config, ruleset = build_shacl_config(self.workflow, form, step=None)

        assert ruleset.ruleset_type == RulesetType.SHACL
        assert "PersonShape" in ruleset.rules_text
        # File-boundary comment marks the inline source.
        assert "<inline>" in ruleset.rules_text

    def test_builder_persists_engine_knobs_to_metadata(self):
        """Engine knobs and result handling land on ruleset metadata.

        The engine reads these from Ruleset.metadata at validation time,
        so they must round-trip through the builder cleanly.
        """
        form = self._bound_form(
            inference_mode="owlrl",
            submission_format="jsonld",
            shacl_result_handling=SHACL_RESULT_REPORT_ONLY,
        )
        config, ruleset = build_shacl_config(self.workflow, form, step=None)

        assert ruleset.metadata["inference_mode"] == "owlrl"
        assert ruleset.metadata["advanced_shacl"] is True
        assert ruleset.metadata["submission_format"] == "jsonld"
        assert ruleset.metadata["shacl_result_handling"] == SHACL_RESULT_REPORT_ONLY
        assert config["shacl_result_handling"] == SHACL_RESULT_REPORT_ONLY

    def test_builder_writes_empty_bundled_standards_in_phase_1(self):
        """Without the checkbox UI, the builder always writes an empty list.

        The metadata key still exists (the engine reads it) — it's just
        always empty until Phase 2 reintroduces the bundle checkboxes.
        Re-add the positive assertion when the bundle content ships.
        """
        form = self._bound_form()
        _, ruleset = build_shacl_config(self.workflow, form, step=None)

        assert ruleset.metadata["bundled_standards"] == []

    def test_builder_records_per_file_sha256_when_uploading(self):
        """Each uploaded shapes file gets {name, size_bytes, sha256} metadata.

        The sha256 is what the signed-attestation payload references,
        so the workflow's evidence can prove exactly which shapes were
        used.
        """
        form = ShaclStepConfigForm(
            data={
                "name": "Step name",
                "inference_mode": "rdfs",
                "advanced_shacl": True,
                "submission_format": "auto",
            },
            files={
                "shapes_files": _shapes_file("223p-shapes.ttl", VALID_SHAPES),
            },
        )
        assert form.is_valid(), form.errors

        _, ruleset = build_shacl_config(self.workflow, form, step=None)

        files_meta = ruleset.metadata["shape_files"]
        assert len(files_meta) == 1
        assert files_meta[0]["name"] == "223p-shapes.ttl"
        assert files_meta[0]["size_bytes"] > 0
        assert len(files_meta[0]["sha256"]) == SHA256_HEX_LENGTH

    def test_builder_returns_config_dict_with_preview(self):
        """The returned config dict carries a shapes_text_preview snippet.

        The step editor shows this preview in the right panel so authors
        can verify what's saved without re-downloading the whole shapes
        file.
        """
        form = self._bound_form()
        config, _ = build_shacl_config(self.workflow, form, step=None)

        assert "shapes_text_preview" in config
        assert "PersonShape" in config["shapes_text_preview"]
        # Preview is capped per the builder; ensure it's not the full
        # rules_text (defensive — the cap is currently 1200 chars).
        assert len(config["shapes_text_preview"]) <= SHAPES_PREVIEW_MAX_CHARS

    def test_builder_concatenates_files_with_boundary_comments(self):
        """Multi-file uploads concatenate with ``# === File: NAME ===`` markers.

        The boundary comments are Turtle comments (ignored by rdflib)
        but they let an operator viewing rules_text in admin see where
        each upload begins. Useful for debugging "which file added
        which shape" without re-running the workflow.
        """
        form = ShaclStepConfigForm(
            data={
                "name": "Step name",
                "inference_mode": "rdfs",
                "advanced_shacl": True,
                "submission_format": "auto",
            },
            files={
                "shapes_files": [
                    _shapes_file("file-a.ttl", VALID_SHAPES),
                    _shapes_file("file-b.ttl", VALID_SHAPES),
                ],
            },
        )
        assert form.is_valid(), form.errors

        _, ruleset = build_shacl_config(self.workflow, form, step=None)

        assert "# === File: file-a.ttl" in ruleset.rules_text
        assert "# === File: file-b.ttl" in ruleset.rules_text

    def test_builder_inlines_library_default_ruleset_snapshot(self):
        """Library SHACL validators are snapshotted into the step ruleset.

        This prevents a later edit to the library validator's
        default_ruleset from silently changing an already-authored
        workflow step. The engine still supports the legacy live-merge
        path for older steps without this metadata flag.
        """
        library_ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.SHACL,
            rules_text=LIBRARY_SHAPES,
            metadata={"ontology_text": VALID_ONTOLOGY},
        )
        library_validator = ValidatorFactory(
            org=self.org,
            is_system=False,
            validation_type=ValidationType.SHACL,
            default_ruleset=library_ruleset,
        )
        form = self._bound_form()

        config, ruleset = build_shacl_config(
            self.workflow,
            form,
            step=None,
            validator=library_validator,
        )

        assert "LibraryShape" in ruleset.rules_text
        assert "PersonShape" in ruleset.rules_text
        assert "ex:Person a rdfs:Class" in ruleset.metadata["ontology_text"]
        assert ruleset.metadata["library_default_inlined"] is True
        snapshot = ruleset.metadata["library_default_snapshot"]
        assert snapshot["default_ruleset_id"] == library_ruleset.pk
        assert len(snapshot["rules_sha256"]) == SHA256_HEX_LENGTH
        assert config["library_default_snapshot"]["default_ruleset_id"] == (
            library_ruleset.pk
        )

    def test_builder_ontology_only_edit_preserves_shapes(self):
        """Updating ontology context should not require re-uploading shapes."""

        ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.SHACL,
            rules_text=VALID_SHAPES,
            metadata={
                "shape_files": [],
                "has_inline_shapes": True,
                "ontology_text": "",
                "ontology_files": [],
                "has_inline_ontology": False,
            },
        )
        step = WorkflowStepFactory(
            workflow=self.workflow,
            ruleset=ruleset,
            config={
                "shape_files": [],
                "ontology_files": [],
                "shapes_text_preview": VALID_SHAPES[:SHAPES_PREVIEW_MAX_CHARS],
            },
        )
        new_ontology = VALID_ONTOLOGY + "\nex:Building a rdfs:Class .\n"
        form = ShaclStepConfigForm(
            data={
                "name": "Step name",
                "ontology_text": new_ontology,
                "inference_mode": "rdfs",
                "advanced_shacl": True,
                "submission_format": "auto",
            },
            step=step,
        )
        assert form.is_valid(), form.errors

        config, updated_ruleset = build_shacl_config(self.workflow, form, step=step)
        updated_ruleset.refresh_from_db()

        assert "PersonShape" in updated_ruleset.rules_text
        assert "ex:Building" in updated_ruleset.metadata["ontology_text"]
        assert config["shapes_text_preview"] == step.config["shapes_text_preview"]
