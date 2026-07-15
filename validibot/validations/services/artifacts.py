"""Register step output artifacts and expose JSON-safe references.

Artifacts are the data-plane counterpart to small step values. Validator
envelopes may report generated files; this module records them in the
run-scoped artifact index and returns compact ``ArtifactRef`` dictionaries for
``steps.<step_key>.artifact.*`` contexts.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from urllib.parse import urlparse

from validibot_shared.validations.artifacts import ARTIFACT_REF_SCHEMA_VERSION
from validibot_shared.validations.artifacts import ArtifactRef

from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.models import Artifact
from validibot.validations.models import StepIODefinition
from validibot.validations.models import ValidationStepRun
from validibot.validations.services import artifact_ports

SHA256_CHUNK_SIZE = 1024 * 1024
CONTRACT_KEY_PATTERN = re.compile(r"[^a-z0-9_]+")


def register_output_artifacts(
    *,
    step_run: ValidationStepRun,
    output_envelope: Any,
) -> list[dict[str, Any]]:
    """Persist artifacts reported by a trusted output envelope.

    The output envelope has already been parsed with the trusted validator
    class and checked against the expected run/validator identity before this
    service is called.
    """

    envelope_artifacts = list(getattr(output_envelope, "artifacts", []) or [])
    if not envelope_artifacts:
        return []

    validator = step_run.workflow_step.validator
    declared_ports = _output_artifact_ports_for_step_run(step_run)
    _validate_declared_output_artifact_counts(
        ports=declared_ports,
        envelope_artifacts=envelope_artifacts,
    )
    raw_outputs = getattr(output_envelope, "raw_outputs", None)
    manifest_uri = getattr(raw_outputs, "manifest_uri", "") if raw_outputs else ""

    seen: set[str] = set()
    refs: list[dict[str, Any]] = []
    for position, envelope_artifact in enumerate(envelope_artifacts, start=1):
        name = str(getattr(envelope_artifact, "name", "") or f"artifact-{position}")
        role = str(getattr(envelope_artifact, "type", "") or "")
        media_type = str(getattr(envelope_artifact, "mime_type", "") or "")
        uri = str(getattr(envelope_artifact, "uri", "") or "")
        size_bytes = getattr(envelope_artifact, "size_bytes", None)
        port = _match_declared_output_port(
            ports=declared_ports,
            envelope_artifact=envelope_artifact,
        )

        if port:
            artifact_ports.validate_output_artifact(
                port=port,
                artifact=envelope_artifact,
            )
            contract_key = port.contract_key
            seen.add(contract_key)
            role = port.role or role
            media_type = media_type or port.media_type
            data_format = port.data_format
            artifact_kind = port.artifact_kind or ArtifactKind.FILE
            metadata_source = "declared_output_port"
        else:
            contract_key = _dedup_contract_key(
                _contract_key_from(role or name, fallback=f"artifact_{position}"),
                seen,
            )
            data_format = ""
            artifact_kind = _infer_kind(role=role, media_type=media_type, name=name)
            metadata_source = "output_envelope"

        sha256 = _sha256_for_uri(uri)
        artifact, _created = Artifact.objects.update_or_create(
            validation_run=step_run.validation_run,
            workflow_step=step_run.workflow_step,
            contract_key=contract_key,
            item_key="",
            defaults={
                "org": step_run.validation_run.org,
                "step_run": step_run,
                "label": name[:120],
                "content_type": media_type,
                "file": "",
                "role": role,
                "kind": artifact_kind,
                "data_format": data_format,
                "storage_uri": uri,
                "size_bytes": size_bytes or 0,
                "sha256": sha256,
                "manifest_uri": manifest_uri,
                "producer_validator_type": validator.validation_type,
                "producer_validator_version": str(validator.version),
                "producer_backend_image_digest": (
                    step_run.validator_backend_image_digest or ""
                ),
                "retention_class": step_run.validation_run.output_retention_policy,
                "metadata": {
                    "source": metadata_source,
                    "envelope_artifact_name": name,
                    "envelope_artifact_type": str(
                        getattr(envelope_artifact, "type", "") or "",
                    ),
                },
            },
        )
        refs.append(build_artifact_ref(artifact).model_dump(mode="json"))

    return refs


def _output_artifact_ports_for_step_run(
    step_run: ValidationStepRun,
) -> list[StepIODefinition]:
    """Return declared output artifact ports for this concrete workflow step."""

    step = step_run.workflow_step
    ports = list(
        step.signal_definitions.filter(
            direction=SignalDirection.OUTPUT,
            io_medium=StepIOMedium.ARTIFACT,
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
        ),
    )
    ports.extend(
        step.validator.signal_definitions.filter(
            direction=SignalDirection.OUTPUT,
            io_medium=StepIOMedium.ARTIFACT,
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
        ),
    )
    return ports


def _match_declared_output_port(
    *,
    ports: list[StepIODefinition],
    envelope_artifact,
) -> StepIODefinition | None:
    """Find the declared output port that corresponds to an envelope artifact."""

    role = str(getattr(envelope_artifact, "type", "") or "")
    name = str(getattr(envelope_artifact, "name", "") or "")
    normalized_role = _contract_key_from(role, fallback="")
    normalized_name = _contract_key_from(name, fallback="")

    for port in ports:
        if port.role and role == port.role:
            return port

    for port in ports:
        if normalized_role and normalized_role == port.contract_key:
            return port
        if normalized_name and normalized_name == port.contract_key:
            return port

    return None


def _validate_declared_output_artifact_counts(
    *,
    ports: list[StepIODefinition],
    envelope_artifacts: list[Any],
) -> None:
    """Validate per-port output artifact counts before mutating the index."""

    for port in ports:
        count = sum(
            1
            for artifact in envelope_artifacts
            if _match_declared_output_port(ports=[port], envelope_artifact=artifact)
        )
        if count == 0 and not port.min_items:
            continue
        artifact_ports.validate_cardinality(
            port=port,
            count=count,
            source_description="output envelope artifacts",
        )


def build_step_artifact_refs(step_run: ValidationStepRun) -> dict[str, Any]:
    """Return artifact refs produced by ``step_run`` keyed by contract key."""

    refs: dict[str, Any] = {}
    prefetched = getattr(step_run, "_prefetched_objects_cache", {})
    artifacts = prefetched.get("artifacts")
    if artifacts is None:
        artifacts = step_run.artifacts.order_by("contract_key", "item_key", "pk")
    for artifact in artifacts:
        if not artifact.contract_key:
            continue
        ref = build_artifact_ref(artifact).model_dump(mode="json")
        if artifact.item_key:
            refs.setdefault(artifact.contract_key, {})[artifact.item_key] = ref
        else:
            refs[artifact.contract_key] = ref
    return refs


def build_artifact_ref(artifact: Artifact) -> ArtifactRef:
    """Build the canonical v1 reference object for an artifact row."""

    uri = artifact.storage_uri or artifact.file.name
    filename = _filename_from_uri(uri) or artifact.label
    step_run_id = str(artifact.step_run_id or "")
    producer_step_key = ""
    if artifact.workflow_step_id:
        producer_step_key = artifact.workflow_step.step_key or ""

    return ArtifactRef(
        schema_version=ARTIFACT_REF_SCHEMA_VERSION,
        artifact_id=str(artifact.pk),
        run_id=str(artifact.validation_run_id),
        step_run_id=step_run_id,
        producer_step_key=producer_step_key,
        contract_key=artifact.contract_key,
        name=artifact.label,
        role=artifact.role,
        kind=artifact.kind,
        media_type=artifact.content_type,
        data_format=artifact.data_format,
        filename=filename,
        size_bytes=artifact.size_bytes if artifact.size_bytes >= 0 else None,
        sha256=artifact.sha256,
        uri=uri,
        manifest_uri=artifact.manifest_uri,
        manifest_sha256=artifact.manifest_sha256,
        producer_validator_type=artifact.producer_validator_type,
        producer_validator_version=artifact.producer_validator_version,
        producer_backend_image_digest=artifact.producer_backend_image_digest,
        retention_class=artifact.retention_class,
        metadata=artifact.metadata or {},
    )


def _contract_key_from(source: str, *, fallback: str) -> str:
    """Normalize envelope artifact names/types into a CEL-friendly key."""

    candidate = CONTRACT_KEY_PATTERN.sub("_", source.strip().lower()).strip("_")
    return candidate or fallback


def _dedup_contract_key(candidate: str, seen: set[str]) -> str:
    """Return ``candidate`` or a stable suffixed variant not yet used."""

    if candidate not in seen:
        seen.add(candidate)
        return candidate

    suffix = 2
    while f"{candidate}_{suffix}" in seen:
        suffix += 1
    deduped = f"{candidate}_{suffix}"
    seen.add(deduped)
    return deduped


def _infer_kind(*, role: str, media_type: str, name: str) -> str:
    """Best-effort projection from backend labels to artifact kind."""

    lowered = f"{role} {media_type} {name}".lower()
    if "log" in lowered or lowered.endswith(".err"):
        return ArtifactKind.LOG
    if "report" in lowered or "html" in lowered or lowered.endswith(".pdf"):
        return ArtifactKind.REPORT
    if lowered.endswith((".zip", ".tar", ".tar.gz", ".tgz")):
        return ArtifactKind.ARCHIVE
    if "dataset" in lowered or lowered.endswith((".csv", ".sql")):
        return ArtifactKind.DATASET
    return ArtifactKind.FILE


def _sha256_for_uri(uri: str) -> str:
    """Compute SHA-256 for local file URIs; leave remote URIs blank for now."""

    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return ""

    path = Path(unquote(parsed.path))
    if not path.is_file():
        return ""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(SHA256_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _filename_from_uri(uri: str) -> str:
    """Return the final path segment from a URI or storage path."""

    if not uri:
        return ""
    parsed = urlparse(uri)
    path = parsed.path if parsed.scheme else uri
    return Path(unquote(path)).name
