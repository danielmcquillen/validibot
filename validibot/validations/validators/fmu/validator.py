"""
FMU validator.

This validator forwards FMU submissions to container-based validator jobs via
the ExecutionBackend abstraction. The FMU is executed in a containerized
environment with the FMU runtime.

This works across different deployment targets:

- Docker Compose: Docker containers via local socket (synchronous)
- GCP: Cloud Run Jobs (async with callbacks)
- AWS: AWS Batch (future)

## Output Envelope Structure

The FMU validator container produces an ``FMUOutputEnvelope`` (from
validibot_shared.fmu.envelopes) containing:

- outputs.output_values: Dict keyed by catalog slug with simulation outputs
  - Each key is a catalog entry slug (e.g., "indoor_temp_c")
  - Values are the simulation outputs for that step output

These output values are extracted via ``extract_output_values()`` for use in
output-stage assertions (e.g., "indoor_temp_c < 26").

## Parser Facts (Phase 6, ADR-2026-05-22b)

Unlike EnergyPlus where the IDF *is* the submission, FMU runtime
submissions are JSON dicts of input values; the FMU itself is bound to
the validator (library path) or stamped into ``step.config`` (step-level
path) at upload time, not extracted from each submission. To expose
FMU model metadata in the ``i.*`` namespace we read the stamped dict
rather than re-parsing the FMU zip on every run.

The hook looks in two places (P1 from the May 2026 review fixed this):

  1. ``step.config['fmu_introspection']`` — for step-level FMU uploads
     against the system FMU validator. This is the primary product
     path.
  2. ``step.validator.fmu_model.introspection_metadata`` — for library
     FMU validators (user-created via ``create_fmu_validator``).

Facts exposed (see ``services/fmu.PARSER_FACT_SPECS`` for the canonical
list):

  - ``model_name``               — modelDescription.xml ``modelName``
  - ``fmi_version``              — modelDescription.xml ``fmiVersion``
  - ``variable_count``           — total scalar variables
  - ``input_variable_count``     — count of causality=input
  - ``output_variable_count``    — count of causality=output
  - ``parameter_count``          — count of causality=parameter
  - ``has_simulation_defaults``  — True when DefaultExperiment supplied
                                   at least one timing field

These let authors gate dispatch before the container runs (e.g.,
``i.fmi_version == "2.0"``, ``i.input_variable_count > 0``).

The return dict is **filtered** to the catalog-declared parser fact
keys (``PARSER_FACT_KEYS``) so any extra fields living on a stamped
metadata dict (older schema, future additions before the catalog
catches up) cannot leak into ``i.*``. EnergyPlus enforces the same
rule on its output extractor; this preserves the "catalog is the
contract" invariant on both sides.
"""

from __future__ import annotations

import logging
from typing import Any

from validibot.validations.validators.base.advanced import AdvancedValidator

# NOTE: PARSER_FACT_KEYS is imported lazily inside the method body
# (not at module top) because the source module ``services.fmu``
# imports Django models at the top level. ``validators/__init__.py``
# eagerly loads every validator package, so a top-level import here
# would pull Django models into the SHACL subprocess worker (which
# runs ``python -m validibot.validations.validators.shacl.pyshacl_worker``
# without calling ``django.setup()``). That triggers
# ``AppRegistryNotReady``. Lazy import keeps the validator package
# import-safe while still letting runtime evaluation filter to
# catalog keys.

logger = logging.getLogger(__name__)


class FMUValidator(AdvancedValidator):
    """
    FMU (Functional Mock-up Unit) validator.

    Dispatches FMU files to container-based validator jobs via the
    ExecutionBackend. The actual FMU execution runs inside a Docker
    container defined in the ``validibot-validator-backends`` repository.

    Overrides:

    - ``extract_output_values()`` — extracts per-variable output values
      for output-stage assertions.
    - ``extract_input_values()`` — exposes FMU model metadata (parsed
      from ``modelDescription.xml`` at upload/probe time) in the ``i.*``
      namespace for input-stage assertions, gating dispatch before
      compute is spent. Works for both library FMU validators and
      step-level FMU uploads against the system validator.

    The shared validate/post_execute_validate lifecycle is handled by
    ``AdvancedValidator``.
    """

    @property
    def validator_display_name(self) -> str:
        return "FMU"

    def extract_input_values(self, payload: Any) -> dict[str, Any] | None:
        """Expose FMU model metadata as input-stage parser facts.

        Resolves the stamped introspection dict from two sources in
        precedence order:

          1. ``step.config['fmu_introspection']`` — step-level FMU
             uploaded against the system FMU validator (primary
             product path). Set by
             ``workflows.views_helpers.build_fmu_config``.
          2. ``step.validator.fmu_model.introspection_metadata`` —
             library FMU validator created via
             ``services.fmu.create_fmu_validator``.

        The ``payload`` arg is the user's input-values JSON, which
        holds per-variable values rather than FMU structural metadata,
        and so is intentionally unused — FMU parser facts live on the
        attached resource, not in the submission. Per-variable input
        values flow into ``i.*`` through the separate
        ``StepInputBinding`` resolution path.

        Filtered to ``PARSER_FACT_KEYS`` so any extra fields in a
        stamped metadata dict (older schema, future additions) cannot
        leak into ``i.*`` — the catalog stays authoritative for which
        keys are part of the public contract.

        Returns None when no stamped metadata is reachable (e.g., a
        sync_validators run with no run context, or a unit test that
        constructed the validator directly). The catalog's
        ``on_missing="null"`` policy keeps ``i.*`` cleanly empty in
        that case rather than raising.

        This is an instance method (not a classmethod, despite the
        base signature originally being ``@classmethod``) so it can
        reach ``self.run_context`` for the per-step/per-validator
        metadata lookup. All existing callers invoke this via
        ``self.extract_input_values(payload)`` so the conversion is
        backwards-compatible.
        """
        metadata = self._resolve_introspection_metadata()
        if metadata is None:
            return None
        # Filter to the declared catalog keys so extras can't leak
        # into i.*. Preserves the "catalog is the contract" rule that
        # extract_output_values also enforces on EnergyPlus outputs.
        # Lazy import — see module-level NOTE above.
        from validibot.validations.services.fmu import PARSER_FACT_KEYS

        return {k: v for k, v in metadata.items() if k in PARSER_FACT_KEYS}

    def _resolve_introspection_metadata(self) -> dict[str, Any] | None:
        """Return the stamped introspection dict for this run, or None.

        Order of preference:

          1. Step-owned ``step.config['fmu_introspection']`` (primary
             product path: system FMU validator + step-level FMU
             upload).
          2. Validator-owned ``fmu_model.introspection_metadata``
             (library FMU validator path).

        Returns None when neither source is reachable so the caller
        can defer to the catalog's ``on_missing`` policy.
        """
        run_context = getattr(self, "run_context", None)
        if run_context is None:
            return None
        step = getattr(run_context, "step", None)
        if step is None:
            return None

        # Step-level path: workflows.views_helpers.build_fmu_config
        # writes the introspection dict into step.config alongside
        # fmu_simulation. This is the primary product path.
        config = getattr(step, "config", None) or {}
        step_metadata = config.get("fmu_introspection")
        if isinstance(step_metadata, dict):
            return dict(step_metadata)  # defensive copy

        # Library path: per-FMU validator created via
        # create_fmu_validator carries the introspection metadata on
        # the FMUModel FK'd from Validator.
        validator = getattr(step, "validator", None)
        if validator is None:
            return None
        fmu_model = getattr(validator, "fmu_model", None)
        if fmu_model is None:
            return None
        metadata = getattr(fmu_model, "introspection_metadata", None)
        if not isinstance(metadata, dict):
            return None
        return dict(metadata)  # defensive copy

    def extract_output_values(self, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract output values from an FMU output envelope.

        FMU envelopes (FMUOutputEnvelope from validibot_shared) store simulation
        outputs in outputs.output_values as a dict keyed by catalog slug.

        Declared as an instance method to match the base contract — FMU
        doesn't currently need the run context for extraction, but
        keeping the signature consistent across advanced validators
        avoids surprises for future maintainers.

        Args:
            output_envelope: FMUOutputEnvelope from the validator container.

        Returns:
            Dict of output values keyed by catalog slug. Returns None if
            extraction fails.
        """
        try:
            outputs = getattr(output_envelope, "outputs", None)
            if not outputs:
                return None

            output_values = getattr(outputs, "output_values", None)
            if not output_values:
                return None

            # Handle Pydantic model
            if hasattr(output_values, "model_dump"):
                return output_values.model_dump(mode="json")

            # Handle plain dict
            if isinstance(output_values, dict):
                return output_values
        except Exception:
            logger.debug("Could not extract step output values from FMU envelope")

        return None
