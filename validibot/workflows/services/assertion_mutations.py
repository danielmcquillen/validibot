"""Centralized mutation helpers for workflow-step assertions.

Assertion rows are part of a workflow version's validation contract. Views,
admin tools, and future APIs should not bypass ``RulesetAssertion.clean()``
with direct ``QuerySet.update()``, ``objects.create()``, or ``delete()`` calls.
This module is the single write path for create/update/delete/reorder so
locked or already-run workflow versions remain reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import models
from django.db import transaction
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from validibot.validations.constants import CatalogRunStage
from validibot.validations.models import Ruleset
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import SignalDefinition


@dataclass(frozen=True)
class AssertionMutationPayload:
    """Normalized fields written to ``RulesetAssertion`` from a form/API."""

    assertion_type: str
    operator: str
    target_signal_definition: SignalDefinition | None
    target_data_path: str
    severity: str
    when_expression: str
    rhs: dict[str, Any]
    options: dict[str, Any]
    message_template: str
    success_message: str
    cel_cache: str


class AssertionMutationService:
    """Single write API for step-level ``RulesetAssertion`` rows."""

    @classmethod
    def create_from_cleaned_data(
        cls,
        *,
        ruleset: Ruleset,
        cleaned_data: dict[str, Any],
    ) -> RulesetAssertion:
        """Create one assertion after model-level contract validation."""

        stage = cls.resolve_stage(cleaned_data)
        payload = cls.payload_from_cleaned_data(cleaned_data)
        with transaction.atomic():
            max_order = (
                ruleset.assertions.filter(cls.stage_filter(stage)).aggregate(
                    max_order=models.Max("order"),
                )["max_order"]
                or 0
            )
            assertion = RulesetAssertion(
                ruleset=ruleset,
                order=max_order + 10,
                **payload.__dict__,
            )
            assertion.full_clean()
            assertion.save()
        return assertion

    @classmethod
    def update_from_cleaned_data(
        cls,
        *,
        assertion: RulesetAssertion,
        cleaned_data: dict[str, Any],
    ) -> RulesetAssertion:
        """Update one assertion after model-level contract validation."""

        payload = cls.payload_from_cleaned_data(cleaned_data)
        for field, value in payload.__dict__.items():
            setattr(assertion, field, value)
        assertion.full_clean()
        assertion.save()
        return assertion

    @staticmethod
    def delete(*, assertion: RulesetAssertion) -> None:
        """Delete one assertion unless its ruleset is locked in use."""

        if assertion.ruleset.is_used_by_locked_workflow():
            raise ValidationError(
                _(
                    "Cannot delete this assertion: it belongs to a ruleset "
                    "used by a workflow that has runs (or is locked). "
                    "Create a new workflow version before changing the "
                    "validation contract.",
                ),
            )
        assertion.delete()

    @classmethod
    def move(
        cls,
        *,
        ruleset: Ruleset,
        assertion: RulesetAssertion,
        direction: str | None,
        use_stage_buckets: bool,
    ) -> bool:
        """Move an assertion up/down in display order.

        Reordering is intentionally allowed on locked workflow versions because
        ``RulesetAssertion.order`` is not a semantic field and does not change
        what historical runs validated.
        """

        assertions = list(ruleset.assertions.order_by("order", "pk"))
        if use_stage_buckets:
            assertions = cls._reordered_within_stage(
                assertions=assertions,
                assertion=assertion,
                direction=direction,
            )
        else:
            assertions = cls._reordered_linear(
                assertions=assertions,
                assertion=assertion,
                direction=direction,
            )
        if assertions is None:
            return False

        with transaction.atomic():
            for pos, item in enumerate(assertions, start=1):
                item.order = pos * 10
            RulesetAssertion.objects.bulk_update(assertions, ["order"])
        return True

    @staticmethod
    def payload_from_cleaned_data(
        cleaned_data: dict[str, Any],
    ) -> AssertionMutationPayload:
        """Convert validated form data into model fields."""

        return AssertionMutationPayload(
            assertion_type=cleaned_data["assertion_type"],
            operator=cleaned_data["resolved_operator"],
            target_signal_definition=cleaned_data.get("resolved_signal"),
            target_data_path=cleaned_data.get("target_data_path_value") or "",
            severity=cleaned_data["severity"],
            when_expression=cleaned_data.get("when_expression") or "",
            rhs=cleaned_data["rhs_payload"],
            options=cleaned_data["options_payload"],
            message_template=cleaned_data.get("message_template") or "",
            success_message=cleaned_data.get("success_message") or "",
            cel_cache=cleaned_data.get("cel_cache") or "",
        )

    @staticmethod
    def resolve_stage(cleaned_data: dict[str, Any]) -> str:
        """Resolve the run stage used for append-order bucketing."""

        resolved_stage = cleaned_data.get("resolved_stage")
        if resolved_stage:
            return resolved_stage
        signal = cleaned_data.get("resolved_signal")
        if signal and getattr(signal, "direction", None):
            return signal.direction
        return CatalogRunStage.OUTPUT

    @staticmethod
    def stage_filter(stage: str) -> Q:
        """Return the queryset filter that matches a stage bucket."""

        if stage == CatalogRunStage.INPUT:
            return Q(target_signal_definition__direction=CatalogRunStage.INPUT)
        return Q(
            Q(target_signal_definition__direction=CatalogRunStage.OUTPUT)
            | Q(target_signal_definition__isnull=True),
        )

    @classmethod
    def _reordered_within_stage(
        cls,
        *,
        assertions: list[RulesetAssertion],
        assertion: RulesetAssertion,
        direction: str | None,
    ) -> list[RulesetAssertion] | None:
        grouped = {CatalogRunStage.INPUT: [], CatalogRunStage.OUTPUT: []}
        for item in assertions:
            key = (
                CatalogRunStage.INPUT
                if item.resolved_run_stage == CatalogRunStage.INPUT
                else CatalogRunStage.OUTPUT
            )
            grouped[key].append(item)

        target_key = (
            CatalogRunStage.INPUT
            if assertion.resolved_run_stage == CatalogRunStage.INPUT
            else CatalogRunStage.OUTPUT
        )
        moved = cls._move_in_list(grouped[target_key], assertion, direction)
        if moved is None:
            return None
        grouped[target_key] = moved
        return grouped[CatalogRunStage.INPUT] + grouped[CatalogRunStage.OUTPUT]

    @classmethod
    def _reordered_linear(
        cls,
        *,
        assertions: list[RulesetAssertion],
        assertion: RulesetAssertion,
        direction: str | None,
    ) -> list[RulesetAssertion] | None:
        return cls._move_in_list(assertions, assertion, direction)

    @staticmethod
    def _move_in_list(
        assertions: list[RulesetAssertion],
        assertion: RulesetAssertion,
        direction: str | None,
    ) -> list[RulesetAssertion] | None:
        try:
            index = assertions.index(assertion)
        except ValueError:
            return None

        if direction == "up" and index > 0:
            assertions[index - 1], assertions[index] = (
                assertions[index],
                assertions[index - 1],
            )
            return assertions
        if direction == "down" and index < len(assertions) - 1:
            assertions[index], assertions[index + 1] = (
                assertions[index + 1],
                assertions[index],
            )
            return assertions
        return None


__all__ = ["AssertionMutationPayload", "AssertionMutationService"]
