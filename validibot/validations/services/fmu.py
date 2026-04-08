from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING

from defusedxml import ElementTree as ET  # noqa: N817
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from slugify import slugify
from validibot_shared.fmu import FMUProbeResult as FMUProbeResultSchema
from validibot_shared.fmu import FMUVariableMeta

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import FMUProbeStatus
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.constants import SignalSourceKind
from validibot.validations.constants import ValidationType
from validibot.validations.models import FMUModel
from validibot.validations.models import FMUProbeResult
from validibot.validations.models import FMUVariable
from validibot.validations.models import SignalDefinition
from validibot.validations.models import Validator
from validibot.validations.signal_metadata.metadata import FMUProviderBinding
from validibot.validations.signal_metadata.metadata import FMUSignalMetadata

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.core.files.uploadedfile import UploadedFile

    from validibot.users.models import Organization


MAX_FMU_SIZE_BYTES = 50 * 1024 * 1024
DISALLOWED_EXTENSIONS = {
    ".exe",
    ".bat",
    ".sh",
    ".cmd",
}


# ---------------------------------------------------------------------------
# Introspection data types
# ---------------------------------------------------------------------------
# These are plain dataclasses (not Django models) so that introspection
# results can be consumed by both:
#   - the library-validator flow (converts to FMUVariable + SignalDefinition rows)
#   - the step-level flow (converts to step config JSON dicts)


@dataclass
class FMUVariableInfo:
    """Parsed metadata for a single FMU variable.

    A plain data object (not a Django model instance) so it can be
    used by both the library-validator and step-level flows without
    coupling to the database layer.
    """

    name: str
    causality: str
    variability: str = ""
    value_reference: int = 0
    value_type: str = "Real"
    unit: str = ""
    description: str = ""


@dataclass
class FMUSimulationDefaults:
    """Default simulation settings from the FMU's DefaultExperiment element.

    These are optional — not all FMUs include a DefaultExperiment.
    When present, they serve as pre-populated defaults in the step
    config form.  The workflow author can override any of them.
    """

    start_time: float | None = None
    stop_time: float | None = None
    step_size: float | None = None
    tolerance: float | None = None


@dataclass
class FMUIntrospectionResult:
    """Result of introspecting an FMU archive.

    Contains everything needed by both the library-validator flow
    (which creates FMUModel + FMUVariable rows) and the step-level
    flow (which stores variable metadata in step config).
    """

    model_name: str
    fmi_version: str
    variables: list[FMUVariableInfo] = field(default_factory=list)
    simulation_defaults: FMUSimulationDefaults = field(
        default_factory=FMUSimulationDefaults,
    )
    checksum: str = ""


class FMUIntrospectionError(ValueError):
    """Raised when an FMU cannot be parsed or introspected."""


class FMUStorageError(ValueError):
    """Raised when FMU files cannot be stored or accessed."""


# ---------------------------------------------------------------------------
# Shared introspection layer
# ---------------------------------------------------------------------------


def _parse_model_description(
    xml_text: str,
) -> tuple[str, str, list[FMUVariableInfo], FMUSimulationDefaults]:
    """Parse modelDescription.xml into variable info and simulation defaults.

    Returns ``(model_name, fmi_version, variables, simulation_defaults)``.
    This is a pure parsing function with no side effects or database access.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FMUIntrospectionError("Unable to parse modelDescription.xml.") from exc

    fmi_version = root.attrib.get("fmiVersion", "2.0")
    model_name = root.attrib.get("modelName", "fmu")
    ns_prefix = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""

    # -- ScalarVariable extraction --
    tag_name = f"{{{ns_prefix}}}ScalarVariable" if ns_prefix else "ScalarVariable"
    variables: list[FMUVariableInfo] = []
    for node in root.iter(tag_name):
        attrs = node.attrib
        name = attrs.get("name") or ""
        if not name:
            continue
        value_type = next(
            (child.tag.split("}", 1)[-1] for child in node if child.tag),
            "Real",
        )
        variables.append(
            FMUVariableInfo(
                name=name,
                causality=attrs.get("causality", "unknown"),
                variability=attrs.get("variability", ""),
                value_reference=int(attrs.get("valueReference") or 0),
                value_type=value_type,
                unit=attrs.get("unit", ""),
                description=attrs.get("description", ""),
            ),
        )

    # -- DefaultExperiment extraction --
    de_tag = f"{{{ns_prefix}}}DefaultExperiment" if ns_prefix else "DefaultExperiment"
    sim_defaults = FMUSimulationDefaults()
    de_node = root.find(de_tag)
    if de_node is not None:
        de_attrs = de_node.attrib
        if "startTime" in de_attrs:
            sim_defaults.start_time = float(de_attrs["startTime"])
        if "stopTime" in de_attrs:
            sim_defaults.stop_time = float(de_attrs["stopTime"])
        if "stepSize" in de_attrs:
            sim_defaults.step_size = float(de_attrs["stepSize"])
        if "tolerance" in de_attrs:
            sim_defaults.tolerance = float(de_attrs["tolerance"])

    return model_name, fmi_version, variables, sim_defaults


def introspect_fmu(payload: bytes, filename: str) -> FMUIntrospectionResult:
    """Validate and introspect an FMU archive.

    Performs structural validation (ZIP, modelDescription.xml presence,
    no dangerous files), parses variable metadata from ScalarVariables,
    and extracts DefaultExperiment settings.

    Used by both:
    - ``create_fmu_validator()`` (library flow) — results feed into
      FMUModel + FMUVariable + SignalDefinition creation.
    - ``build_fmu_config()`` (step-level flow) — results feed into
      ``sync_step_fmu_signals()`` and ``step.config["fmu_simulation"]``.

    Raises ``FMUIntrospectionError`` on validation failure.
    """
    display_name = filename or "fmu"
    if len(payload) > MAX_FMU_SIZE_BYTES:
        raise FMUIntrospectionError(
            _("FMU %(name)s exceeds the maximum size of %(limit)s bytes.")
            % {"name": display_name, "limit": MAX_FMU_SIZE_BYTES},
        )
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            names = archive.namelist()
            if "modelDescription.xml" not in names:
                raise FMUIntrospectionError(
                    _("FMU %(name)s is missing modelDescription.xml.")
                    % {"name": display_name},
                )
            for name in names:
                member = name.lower()
                suffix = Path(member).suffix
                if member.startswith(("../", "/")):
                    raise FMUIntrospectionError(
                        _("FMU %(name)s contains unsafe path entries.")
                        % {"name": display_name},
                    )
                if suffix in DISALLOWED_EXTENSIONS:
                    raise FMUIntrospectionError(
                        _("FMU %(name)s contains disallowed file %(file)s.")
                        % {"name": display_name, "file": name},
                    )
            with archive.open("modelDescription.xml") as handle:
                xml_text = handle.read().decode("utf-8")
    except zipfile.BadZipFile as exc:
        raise FMUIntrospectionError(
            _("FMU %(name)s is not a valid zip archive.") % {"name": display_name}
        ) from exc
    except UnicodeDecodeError as exc:
        raise FMUIntrospectionError(
            _("modelDescription.xml in %(name)s is not UTF-8 text.")
            % {"name": display_name}
        ) from exc

    model_name, fmi_version, variables, sim_defaults = _parse_model_description(
        xml_text,
    )
    checksum = hashlib.sha256(payload).hexdigest()
    return FMUIntrospectionResult(
        model_name=model_name,
        fmi_version=fmi_version,
        variables=variables,
        simulation_defaults=sim_defaults,
        checksum=checksum,
    )


def _data_type_for_variable(value_type: str) -> str:
    vt = (value_type or "").lower()
    if vt in {"real", "integer", "enumeration"}:
        return CatalogValueType.NUMBER
    if vt == "boolean":
        return CatalogValueType.BOOLEAN
    if vt == "string":
        return CatalogValueType.STRING
    return CatalogValueType.OBJECT


def _direction_for_causality(causality: str) -> str | None:
    """Map FMU causality to signal direction, or None for unsupported types."""
    lowered = (causality or "").lower()
    if lowered == "input":
        return SignalDirection.INPUT
    if lowered == "output":
        return SignalDirection.OUTPUT
    return None


def _should_use_cloud_storage() -> bool:
    """
    Determine whether FMUs should be uploaded to cloud storage or stored locally.

    Local/dev runs should keep files on the filesystem; production/staging
    enables cloud storage by configuring GCS_VALIDATION_BUCKET (GCP) or
    equivalent for other platforms.
    """

    return bool(settings.GCS_VALIDATION_BUCKET)


def _upload_fmu_to_cloud_storage(checksum: str, payload: bytes) -> str:
    """
    Upload a validated FMU payload to cloud storage and return its URI.

    Currently supports GCS (gs:// URIs). Uses checksum-based naming to
    deduplicate identical FMUs.
    """

    from google.cloud import storage

    bucket_name = settings.GCS_VALIDATION_BUCKET
    if not bucket_name:
        msg = "Cloud storage bucket is not configured (GCS_VALIDATION_BUCKET)."
        raise FMUStorageError(msg)

    object_path = f"fmus/{checksum}.fmu"
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_path)
    blob.upload_from_string(payload, content_type="application/octet-stream")
    return f"gs://{bucket_name}/{object_path}"


def _variable_info_to_model(info: FMUVariableInfo) -> FMUVariable:
    """Convert a plain FMUVariableInfo dataclass to an unsaved FMUVariable model."""
    return FMUVariable(
        name=info.name,
        causality=info.causality,
        variability=info.variability,
        value_reference=info.value_reference,
        value_type=info.value_type,
        unit=info.unit,
    )


def create_fmu_validator(
    *,
    org: Organization,
    project,
    name: str,
    upload: UploadedFile,
    short_description: str = "",
    description: str = "",
    approve_immediately: bool = True,
    storage_backend=None,
) -> Validator:
    """
    Create an FMU validator, parse the uploaded FMU, and seed catalog entries.

    Uses the shared ``introspect_fmu()`` layer for validation and metadata
    extraction, then converts the results into Django model instances
    (FMUModel, FMUVariable, SignalDefinition).

    When ``approve_immediately`` is False the FMU will remain unapproved until a
    probe run is completed. ``storage_backend`` can be supplied to stream the
    upload to S3 or other storage providers; by default Django's FileField
    storage is used.
    """

    with transaction.atomic():
        raw_bytes = upload.read()
        result = introspect_fmu(payload=raw_bytes, filename=upload.name)
        wrapped_upload = ContentFile(raw_bytes, name=upload.name)
        fmu = FMUModel.objects.create(
            org=org,
            project=project,
            name=name,
            description=description,
            size_bytes=len(raw_bytes),
            checksum=result.checksum,
            gcs_uri=_upload_fmu_to_cloud_storage(result.checksum, raw_bytes)
            if _should_use_cloud_storage()
            else "",
        )
        try:
            stored_file = (
                wrapped_upload
                if storage_backend is None
                else storage_backend(wrapped_upload)
            )
            # Save the FMU payload to the configured storage to ensure a local path
            # exists in dev/test and an object exists in cloud storage when enabled.
            fmu.file.save(upload.name, stored_file, save=False)
        except Exception as exc:  # pragma: no cover - storage failures are surfaced
            raise FMUStorageError(str(exc)) from exc
        fmu.fmu_version = result.fmi_version
        fmu.introspection_metadata = {
            "model_name": result.model_name,
            "variable_count": len(result.variables),
        }
        fmu.is_approved = approve_immediately
        fmu.save()

        validator = Validator.objects.create(
            org=org,
            name=name,
            slug=slugify(f"{org.id}-{name}"),
            short_description=short_description,
            description=description,
            validation_type=ValidationType.FMU,
            has_processor=True,
            fmu_model=fmu,
            supported_file_types=[SubmissionFileType.BINARY],
            supported_data_formats=[SubmissionDataFormat.FMU],
            supports_assertions=True,
        )

        model_variables = [_variable_info_to_model(info) for info in result.variables]
        _persist_variables(fmu, validator, model_variables)
        FMUProbeResult.objects.create(
            fmu_model=fmu,
            status=FMUProbeStatus.SUCCEEDED
            if approve_immediately
            else FMUProbeStatus.PENDING,
            last_error="" if approve_immediately else _("Awaiting probe run."),
            details={"variable_count": len(result.variables)},
        )
    return validator


def _persist_variables(
    fmu_model: FMUModel,
    validator: Validator,
    variables: Iterable[FMUVariable],
) -> None:
    """Persist FMUVariable model instances and create matching signal definitions."""
    prepared: list[FMUVariable] = []
    for var in variables:
        var.fmu_model = fmu_model
        prepared.append(var)
    FMUVariable.objects.bulk_create(prepared)
    existing_keys = set(
        validator.signal_definitions.values_list("contract_key", flat=True),
    )
    for var in prepared:
        direction = _direction_for_causality(var.causality)
        if not direction:
            continue
        base_key = slugify(var.name, separator="_") or "signal"
        key = base_key
        counter = 2
        while key in existing_keys:
            key = f"{base_key}-{counter}"
            counter += 1
        existing_keys.add(key)
        SignalDefinition.objects.get_or_create(
            validator=validator,
            contract_key=key,
            direction=direction,
            defaults={
                "native_name": var.name,
                "origin_kind": SignalOriginKind.FMU,
                "source_kind": (
                    SignalSourceKind.PAYLOAD_PATH
                    if direction == SignalDirection.INPUT
                    else SignalSourceKind.INTERNAL
                ),
                "is_path_editable": direction == SignalDirection.INPUT,
                "data_type": _data_type_for_variable(var.value_type),
                "provider_binding": FMUProviderBinding(
                    causality=var.causality,
                ).model_dump(),
                "metadata": FMUSignalMetadata(
                    variability=var.variability,
                    value_reference=var.value_reference,
                    value_type=var.value_type,
                ).model_dump(),
            },
        )


def _read_fmu_bytes(fmu_model: FMUModel) -> bytes:
    """
    Read FMU file bytes from local storage or cloud storage.

    Raises FMUIntrospectionError if the file cannot be read.
    """
    # Try local file first
    if fmu_model.file:
        try:
            fmu_model.file.open("rb")
            payload = fmu_model.file.read()
            fmu_model.file.close()
        except Exception:  # noqa: S110 - intentionally silent, will try GCS next
            pass
        else:
            return payload

    # Try GCS if configured
    if fmu_model.gcs_uri:
        from google.cloud import storage

        # Parse gs://bucket/path format
        uri = fmu_model.gcs_uri
        if uri.startswith("gs://"):
            uri = uri[5:]
        parts = uri.split("/", 1)
        bucket_name, object_path = parts[0], parts[1] if len(parts) > 1 else ""
        if not object_path:
            msg = f"Invalid GCS URI: {fmu_model.gcs_uri}"
            raise FMUIntrospectionError(msg)
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_path)
        return blob.download_as_bytes()

    msg = "FMU file not found in local storage or cloud storage."
    raise FMUIntrospectionError(msg)


def run_fmu_probe(
    fmu_model: FMUModel,
    *,
    _return_logs: bool = False,
) -> FMUProbeResultSchema:
    """
    Probe an FMU by parsing its modelDescription.xml and update metadata + catalog.

    This runs in-process (no container needed) since probing just unpacks the ZIP
    and parses XML to extract variable metadata. We use it to populate variables
    and mark the FMU as approved before allowing workflow authors to attach
    assertions.

    The return_logs parameter is kept for API compatibility but unused since
    in-process probing doesn't produce container logs.
    """
    import time

    probe_record, _ = FMUProbeResult.objects.get_or_create(
        fmu_model=fmu_model,
        defaults={"status": FMUProbeStatus.PENDING},
    )
    probe_record.status = FMUProbeStatus.RUNNING
    probe_record.last_error = ""
    probe_record.save(update_fields=["status", "last_error", "modified"])

    start_time = time.monotonic()

    try:
        payload = _read_fmu_bytes(fmu_model)
        result = introspect_fmu(
            payload=payload,
            filename=fmu_model.name or "model.fmu",
        )
    except FMUIntrospectionError as exc:
        probe_record.status = FMUProbeStatus.FAILED
        probe_record.last_error = str(exc)
        probe_record.save(update_fields=["status", "last_error", "modified"])
        fmu_model.is_approved = False
        fmu_model.save(update_fields=["is_approved", "modified"])
        return FMUProbeResultSchema.failure(errors=[str(exc)])
    except Exception as exc:
        probe_record.status = FMUProbeStatus.FAILED
        probe_record.last_error = str(exc)
        probe_record.save(update_fields=["status", "last_error", "modified"])
        fmu_model.is_approved = False
        fmu_model.save(update_fields=["is_approved", "modified"])
        return FMUProbeResultSchema.failure(errors=[str(exc)])

    elapsed = time.monotonic() - start_time

    # Convert FMUVariableInfo (dataclass) to FMUVariableMeta (Pydantic schema)
    variable_metas = [
        FMUVariableMeta(
            name=var.name,
            causality=var.causality,
            variability=var.variability or None,
            value_reference=var.value_reference,
            value_type=var.value_type,
            unit=var.unit or None,
        )
        for var in result.variables
    ]

    # Update FMU metadata
    fmu_model.fmu_version = result.fmi_version
    fmu_model.introspection_metadata = {
        "model_name": result.model_name,
        "variable_count": len(result.variables),
    }

    # Refresh variables and catalog entries from probe results
    _refresh_variables_from_probe(fmu_model, variable_metas)

    # Mark as approved
    probe_record.status = FMUProbeStatus.SUCCEEDED
    probe_record.last_error = ""
    probe_record.details = {"variable_count": len(result.variables)}
    fmu_model.is_approved = True

    probe_record.save(update_fields=["status", "last_error", "details", "modified"])
    fmu_model.save(
        update_fields=[
            "is_approved",
            "fmu_version",
            "introspection_metadata",
            "modified",
        ]
    )

    return FMUProbeResultSchema.success(
        variables=variable_metas,
        execution_seconds=elapsed,
        messages=[
            f"Parsed {len(result.variables)} variables from modelDescription.xml",
        ],
    )


def _refresh_variables_from_probe(
    fmu_model: FMUModel,
    variables: list,
) -> None:
    """
    Update FMU variable rows and refresh signal definitions based on probe output.

    We rebuild variables from the probe response to make sure the signals stay
    aligned with the latest FMU metadata.
    """

    validator = fmu_model.validators.first()
    if validator is None:
        return
    FMUVariable.objects.filter(fmu_model=fmu_model).delete()
    SignalDefinition.objects.filter(validator=validator).delete()
    shaped_vars = [
        FMUVariable(
            fmu_model=fmu_model,
            name=var.name,
            causality=var.causality,
            variability=var.variability or "",
            value_reference=getattr(var, "value_reference", 0) or 0,
            value_type=var.value_type,
            unit=var.unit or "",
        )
        for var in variables
    ]
    _persist_variables(fmu_model, validator, shaped_vars)
