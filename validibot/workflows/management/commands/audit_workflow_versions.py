"""Audit workflow versions for trust-gap evidence.

ADR-2026-04-27 Phase 3 Session D, task 12: surface "legacy
versioning" — workflows that already have runs (or are locked) but
whose dependent rows lack the trust columns Sessions B and C added
(``Validator.semantic_digest``, ``ValidatorResourceFile.content_hash``,
``WorkflowStepResource.content_hash``). Those rows are not
inherently broken; their immutability simply cannot be *proven*.

What the command reports
========================

For every workflow that's "in use" (``is_locked=True`` OR
``validation_runs.exists()``), the audit walks its steps and
records:

1. **Validator gaps** — steps whose validator has an empty
   ``semantic_digest``. These predate Session B's drift detection
   so we cannot prove the validator's behavior matches what past
   runs were checking against.
2. **Validator drift** — steps whose validator's stored
   ``semantic_digest`` differs from the digest re-computed from
   the current config. This means *someone changed the validator's
   semantic config under the same (slug, version)* — likely a
   bypass of the Session B gate (e.g. ``--allow-drift`` was used,
   or a manual DB edit happened).
3. **Catalog resource gaps** — ``ValidatorResourceFile`` rows
   referenced by a step's ``WorkflowStepResource(validator_resource_file=...)``
   whose ``content_hash`` is empty. Predates Session C.
4. **Step-owned resource gaps** — ``WorkflowStepResource`` rows
   in step-owned mode whose ``content_hash`` is empty. Predates
   Session C.
5. **Step-owned resource drift** — step-owned resources whose
   stored ``content_hash`` differs from the current bytes hash.
   Indicates someone replaced bytes outside the gate (e.g. via
   raw filesystem write to media storage).

Each finding has a ``severity``:

- ``info`` — coverage gap. The row is legacy but no drift detected.
- ``warn`` — coverage gap on an *actively-used* (has-runs) workflow.
- ``error`` — drift detected. The actual behaviour and the trust
  column disagree.

Output formats
==============

- **Default (plain text)**: human-readable summary with per-workflow
  detail blocks.
- **``--json``**: machine-readable report against the
  ``validibot.workflow_audit.v1`` schema, suitable for piping into
  CI gates or dashboards.

Exit codes
==========

- ``0`` — no findings, OR only ``info`` / ``warn`` findings
- ``1`` — at least one ``error`` finding

In ``--strict`` mode, ``warn`` also exits non-zero (matches the
``check_validibot`` strict semantics, so operators have one mental
model for "fail CI on this concern").
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Q

from validibot.core.filesafety import sha256_field_file
from validibot.validations.services.validator_digest import compute_semantic_digest

# Severity scale — mirrors the doctor command's vocabulary so
# operators have one mental model for "what does this severity
# mean, do I block CI?".
SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_ERROR = "error"

# JSON schema version. Additive changes preserve v1; removing or
# renaming fields requires v2. See the doctor command's similar
# contract for the rationale.
JSON_SCHEMA_VERSION = "validibot.workflow_audit.v1"


class Command(BaseCommand):
    help = (
        "Audit workflow versions for trust-gap evidence. Reports legacy "
        "rows (no trust column populated) and drift (column disagrees "
        "with current value). Implements ADR-2026-04-27 Phase 3 task 12."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            default=False,
            dest="emit_json",
            help="Emit a JSON report against the validibot.workflow_audit.v1 schema.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            default=False,
            help="Exit non-zero on warn findings as well as errors.",
        )
        parser.add_argument(
            "--include-unused",
            action="store_true",
            default=False,
            help=(
                "Audit all workflows, not just locked / used ones. "
                "Useful for proactive coverage checks before locking."
            ),
        )
        parser.add_argument(
            "--workflow-id",
            type=int,
            default=None,
            help="Audit a single workflow (by primary key).",
        )

    def handle(self, *args, **options) -> None:
        emit_json = options["emit_json"]
        strict = options["strict"]
        include_unused = options["include_unused"]
        workflow_id = options["workflow_id"]

        # Local import — workflows.models is heavy and we only need
        # it inside handle() to keep --help fast.
        from validibot.workflows.models import Workflow

        qs = Workflow.objects.all()
        if workflow_id is not None:
            qs = qs.filter(pk=workflow_id)
        elif not include_unused:
            # "in use" = locked OR has at least one validation run.
            # Use a subquery so the gate is evaluated efficiently in
            # the database rather than per-row in Python.
            from validibot.validations.models import ValidationRun

            has_runs = ValidationRun.objects.filter(workflow=OuterRef("pk"))
            qs = qs.filter(Q(is_locked=True) | Exists(has_runs))

        qs = qs.select_related("org").prefetch_related(
            "steps__validator",
            "steps__step_resources__validator_resource_file",
        )

        report = self._build_report(qs)

        if emit_json:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        else:
            self._render_text(report)

        # Exit code logic.
        has_error = any(
            f["severity"] == SEVERITY_ERROR
            for w in report["workflows"]
            for f in w["findings"]
        )
        has_warn = any(
            f["severity"] == SEVERITY_WARN
            for w in report["workflows"]
            for f in w["findings"]
        )
        if has_error or (strict and has_warn):
            # Use the BaseCommand convention: raising SystemExit
            # cleanly via sys.exit. Tests using ``call_command``
            # check the report dict directly rather than the exit
            # code, so we only exit if invoked from the CLI.
            import sys

            sys.exit(1)

    # ── Report construction ──────────────────────────────────────

    def _build_report(self, queryset) -> dict[str, Any]:
        """Walk ``queryset`` and return a structured report dict.

        The dict shape is the contract documented at the top of this
        module under the ``validibot.workflow_audit.v1`` schema.
        """
        # Pre-compute current digest per known config, keyed by
        # ``(slug, version)``. Avoids a per-step config lookup.
        current_digests = self._build_current_digest_index()

        workflow_reports: list[dict[str, Any]] = []
        # ``chunk_size`` is mandatory when iterating with
        # prefetch_related (Django 5.1+). 100 keeps memory low while
        # avoiding too many round trips for the prefetched joins.
        for workflow in queryset.iterator(chunk_size=100):
            findings = self._audit_workflow(workflow, current_digests)
            if findings:
                workflow_reports.append(
                    {
                        "workflow_id": workflow.pk,
                        "org_id": workflow.org_id,
                        "slug": workflow.slug,
                        "version": workflow.version,
                        "is_locked": workflow.is_locked,
                        "has_runs": workflow.has_runs(),
                        "findings": findings,
                    },
                )

        return {
            "schema": JSON_SCHEMA_VERSION,
            "summary": {
                "workflows_with_findings": len(workflow_reports),
                "total_findings": sum(len(w["findings"]) for w in workflow_reports),
                "by_severity": self._count_by_severity(workflow_reports),
            },
            "workflows": workflow_reports,
        }

    def _build_current_digest_index(self) -> dict[tuple[str, str], str]:
        """Map ``(validator.slug, validator.version)`` to the current digest.

        Reads from the live config registry, not the DB column. This
        is the "what would sync_validators compute right now?"
        snapshot we compare DB rows against.
        """
        from validibot.validations.validators.base.config import get_all_configs

        index: dict[tuple[str, str], str] = {}
        for cfg in get_all_configs():
            digest = compute_semantic_digest(cfg.model_dump())
            index[(cfg.slug, cfg.version)] = digest
        return index

    def _audit_workflow(
        self,
        workflow,
        current_digests: dict[tuple[str, str], str],
    ) -> list[dict[str, Any]]:
        """Return per-workflow findings (validator + resource gaps + drift)."""
        findings: list[dict[str, Any]] = []
        # If the workflow has runs, gaps escalate from info -> warn.
        # The thinking: a locked-but-unrun workflow is staged for
        # use; gaps are easy to fix before the first launch. Once
        # there are runs, those runs are operating against an
        # un-provable contract.
        gap_severity = SEVERITY_WARN if workflow.has_runs() else SEVERITY_INFO

        for step in workflow.steps.all():
            findings.extend(
                self._audit_validator(step, gap_severity, current_digests),
            )
            for resource in step.step_resources.all():
                findings.extend(self._audit_resource(resource, gap_severity))
        return findings

    def _audit_validator(
        self,
        step,
        gap_severity: str,
        current_digests: dict[tuple[str, str], str],
    ) -> list[dict[str, Any]]:
        """Emit findings for a step's validator's trust columns."""
        findings: list[dict[str, Any]] = []
        validator = step.validator
        if validator is None:
            return findings

        if not validator.semantic_digest:
            findings.append(
                {
                    "severity": gap_severity,
                    "code": "VALIDATOR_DIGEST_MISSING",
                    "step_id": step.pk,
                    "validator_id": validator.pk,
                    "message": (
                        f"Validator {validator.slug} v{validator.version!r} "
                        f"has no semantic_digest. Re-run sync_validators to "
                        f"populate, or accept that this workflow's behavior "
                        f"under this validator is legacy versioned."
                    ),
                },
            )
            return findings

        # Compare to current config digest if we have one.
        key = (validator.slug, validator.version)
        current = current_digests.get(key)
        if current and current != validator.semantic_digest:
            findings.append(
                {
                    "severity": SEVERITY_ERROR,
                    "code": "VALIDATOR_DIGEST_DRIFT",
                    "step_id": step.pk,
                    "validator_id": validator.pk,
                    "message": (
                        f"Validator {validator.slug} v{validator.version!r}: "
                        f"stored digest {validator.semantic_digest[:12]}... "
                        f"differs from current config digest "
                        f"{current[:12]}.... The validator's semantic "
                        f"config has been mutated since sync_validators "
                        f"last populated this row."
                    ),
                },
            )
        return findings

    def _audit_resource(
        self,
        resource,
        gap_severity: str,
    ) -> list[dict[str, Any]]:
        """Emit findings for a step resource's hash coverage / drift."""
        findings: list[dict[str, Any]] = []

        if resource.is_catalog_reference:
            catalog = resource.validator_resource_file
            if catalog is None:
                return findings
            if not catalog.content_hash:
                findings.append(
                    {
                        "severity": gap_severity,
                        "code": "CATALOG_RESOURCE_HASH_MISSING",
                        "step_id": resource.step_id,
                        "validator_resource_file_id": str(catalog.id),
                        "message": (
                            f"Catalog resource {catalog.name!r} has no "
                            f"content_hash. Re-save the row to populate "
                            f"or accept legacy versioning."
                        ),
                    },
                )
            return findings

        # Step-owned mode.
        if not resource.step_resource_file:
            return findings

        if not resource.content_hash:
            findings.append(
                {
                    "severity": gap_severity,
                    "code": "STEP_RESOURCE_HASH_MISSING",
                    "step_id": resource.step_id,
                    "resource_id": resource.pk,
                    "message": (
                        f"Step-owned resource (filename={resource.filename!r}) "
                        f"has no content_hash. Re-save the row to populate."
                    ),
                },
            )
            return findings

        # Compute current hash and compare to stored.
        try:
            actual = sha256_field_file(resource.step_resource_file)
        except Exception as exc:
            findings.append(
                {
                    "severity": SEVERITY_WARN,
                    "code": "STEP_RESOURCE_READ_ERROR",
                    "step_id": resource.step_id,
                    "resource_id": resource.pk,
                    "message": (
                        f"Could not read step-owned file "
                        f"({resource.filename!r}): {exc!s}. The drift "
                        f"check could not run."
                    ),
                },
            )
            return findings

        if actual != resource.content_hash:
            findings.append(
                {
                    "severity": SEVERITY_ERROR,
                    "code": "STEP_RESOURCE_HASH_DRIFT",
                    "step_id": resource.step_id,
                    "resource_id": resource.pk,
                    "message": (
                        f"Step-owned resource (filename={resource.filename!r}): "
                        f"stored hash {resource.content_hash[:12]}... differs "
                        f"from current bytes hash {actual[:12]}.... The file "
                        f"has been replaced outside the immutability gate."
                    ),
                },
            )
        return findings

    def _count_by_severity(
        self,
        workflow_reports: list[dict[str, Any]],
    ) -> dict[str, int]:
        counts: dict[str, int] = {
            SEVERITY_INFO: 0,
            SEVERITY_WARN: 0,
            SEVERITY_ERROR: 0,
        }
        for w in workflow_reports:
            for f in w["findings"]:
                counts[f["severity"]] += 1
        return counts

    # ── Plain-text rendering ────────────────────────────────────

    def _render_text(self, report: dict[str, Any]) -> None:
        """Render the report as human-readable plain text."""
        summary = report["summary"]
        by_sev = summary["by_severity"]
        if summary["workflows_with_findings"] == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "No findings — every audited workflow is fully "
                    "covered by trust columns and shows no drift.",
                ),
            )
            return

        self.stdout.write(
            self.style.NOTICE(
                f"Audited {summary['workflows_with_findings']} workflows "
                f"with findings. Total: {summary['total_findings']} "
                f"(error={by_sev[SEVERITY_ERROR]}, "
                f"warn={by_sev[SEVERITY_WARN]}, "
                f"info={by_sev[SEVERITY_INFO]}).",
            ),
        )
        self.stdout.write("")

        for w in report["workflows"]:
            self.stdout.write(
                f"Workflow #{w['workflow_id']} {w['slug']}@v{w['version']} "
                f"(locked={w['is_locked']}, has_runs={w['has_runs']})",
            )
            for f in w["findings"]:
                marker = self._severity_marker(f["severity"])
                self.stdout.write(f"  {marker} [{f['code']}] {f['message']}")
            self.stdout.write("")

    def _severity_marker(self, severity: str) -> str:
        if severity == SEVERITY_ERROR:
            return self.style.ERROR("ERROR")
        if severity == SEVERITY_WARN:
            return self.style.WARNING("WARN ")
        return self.style.NOTICE("INFO ")
