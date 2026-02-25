from __future__ import annotations

import hashlib
import io
import zipfile
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
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import FMUProbeStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import FMUModel
from validibot.validations.models import FMUProbeResult
from validibot.validations.models import FMUVariable
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorCatalogEntry

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


class FMUIntrospectionError(ValueError):
    """Raised when an FMU cannot be parsed or introspected."""


class FMUStorageError(ValueError):
    """Raised when FMU files cannot be stored or accessed."""


def _parse_variables(xml_text: str) -> tuple[str, str, list[FMUVariable]]:
    """Load ScalarVariable entries from modelDescription.xml into FMUVariable shells."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FMUIntrospectionError("Unable to parse modelDescription.xml.") from exc

    fmi_version = root.attrib.get("fmiVersion", "2.0")
    model_name = root.attrib.get("modelName", "fmu")
    ns_prefix = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    tag_name = f"{{{ns_prefix}}}ScalarVariable" if ns_prefix else "ScalarVariable"

    variables: list[FMUVariable] = []
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
            FMUVariable(
                name=name,
                causality=attrs.get("causality", "unknown"),
                variability=attrs.get("variability", ""),
                value_reference=int(attrs.get("valueReference") or 0),
                value_type=value_type,
                unit=attrs.get("unit", ""),
            ),
        )
    return model_name, fmi_version, variables


def _validate_fmu_bytes(
    payload: bytes, filename: str
) -> tuple[str, str, list[FMUVariable], str]:
    """
    Perform structural, safety, and metadata validation on an FMU payload.

    Returns ``(model_name, fmi_version, variables, checksum)`` after verifying
    the archive is a ZIP, contains modelDescription.xml, and does not include
    obviously dangerous file types.
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

    model_name, fmi_version, variables = _parse_variables(xml_text)
    checksum = hashlib.sha256(payload).hexdigest()
    return model_name, fmi_version, variables, checksum


def _data_type_for_variable(value_type: str) -> str:
    vt = (value_type or "").lower()
    if vt in {"real", "integer", "enumeration"}:
        return CatalogValueType.NUMBER
    if vt == "boolean":
        return CatalogValueType.BOOLEAN
    if vt == "string":
        return CatalogValueType.STRING
    return CatalogValueType.OBJECT


def _run_stage_for_causality(causality: str) -> CatalogRunStage | None:
    lowered = (causality or "").lower()
    if lowered == "input":
        return CatalogRunStage.INPUT
    if lowered == "output":
        return CatalogRunStage.OUTPUT
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

    When ``approve_immediately`` is False the FMU will remain unapproved until a
    probe run is completed. ``storage_backend`` can be supplied to stream the
    upload to S3 or other storage providers; by default Django's FileField
    storage is used.
    """

    with transaction.atomic():
        raw_bytes = upload.read()
        model_name, fmi_version, variables, checksum = _validate_fmu_bytes(
            payload=raw_bytes,
            filename=upload.name,
        )
        wrapped_upload = ContentFile(raw_bytes, name=upload.name)
        fmu = FMUModel.objects.create(
            org=org,
            project=project,
            name=name,
            description=description,
            size_bytes=len(raw_bytes),
            checksum=checksum,
            gcs_uri=_upload_fmu_to_cloud_storage(checksum, raw_bytes)
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
        fmu.fmu_version = fmi_version
        fmu.introspection_metadata = {
            "model_name": model_name,
            "variable_count": len(variables),
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
        )

        _persist_variables(fmu, validator, variables)
        FMUProbeResult.objects.create(
            fmu_model=fmu,
            status=FMUProbeStatus.SUCCEEDED
            if approve_immediately
            else FMUProbeStatus.PENDING,
            last_error="" if approve_immediately else _("Awaiting probe run."),
            details={"variable_count": len(variables)},
        )
    return validator


def _persist_variables(
    fmu_model: FMUModel,
    validator: Validator,
    variables: Iterable[FMUVariable],
) -> None:
    prepared: list[FMUVariable] = []
    for var in variables:
        var.fmu_model = fmu_model
        prepared.append(var)
    FMUVariable.objects.bulk_create(prepared)
    existing_slugs = set(validator.catalog_entries.values_list("slug", flat=True))
    for var in prepared:
        run_stage = _run_stage_for_causality(var.causality)
        if not run_stage:
            continue
        base_slug = slugify(var.name, separator="_") or "signal"
        slug = base_slug
        counter = 2
        while slug in existing_slugs:
            slug = f"{base_slug}-{counter}"
            counter += 1
        existing_slugs.add(slug)
        entry = ValidatorCatalogEntry.objects.create(
            validator=validator,
            entry_type=CatalogEntryType.SIGNAL,
            run_stage=run_stage,
            slug=slug,
            label=var.name,
            target_data_path=var.name,
            input_binding_path="",
            data_type=_data_type_for_variable(var.value_type),
            metadata={"fmu_value_type": var.value_type, "unit": var.unit},
            is_required=(run_stage == CatalogRunStage.INPUT),
        )
        FMUVariable.objects.filter(
            fmu_model=fmu_model,
            name=var.name,
        ).update(catalog_entry=entry)


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
        model_name, fmi_version, variables, _checksum = _validate_fmu_bytes(
            payload=payload,
            filename=fmu_model.name or "model.fmu",
        )
    except FMUIntrospectionError as exc:
        elapsed = time.monotonic() - start_time
        probe_record.status = FMUProbeStatus.FAILED
        probe_record.last_error = str(exc)
        probe_record.save(update_fields=["status", "last_error", "modified"])
        fmu_model.is_approved = False
        fmu_model.save(update_fields=["is_approved", "modified"])
        return FMUProbeResultSchema.failure(errors=[str(exc)])
    except Exception as exc:
        elapsed = time.monotonic() - start_time
        probe_record.status = FMUProbeStatus.FAILED
        probe_record.last_error = str(exc)
        probe_record.save(update_fields=["status", "last_error", "modified"])
        fmu_model.is_approved = False
        fmu_model.save(update_fields=["is_approved", "modified"])
        return FMUProbeResultSchema.failure(errors=[str(exc)])

    elapsed = time.monotonic() - start_time

    # Convert FMUVariable (Django model) to FMUVariableMeta (Pydantic schema)
    variable_metas = [
        FMUVariableMeta(
            name=var.name,
            causality=var.causality,
            variability=var.variability or None,
            value_reference=var.value_reference,
            value_type=var.value_type,
            unit=var.unit or None,
        )
        for var in variables
    ]

    # Update FMU metadata
    fmu_model.fmu_version = fmi_version
    fmu_model.introspection_metadata = {
        "model_name": model_name,
        "variable_count": len(variables),
    }

    # Refresh variables and catalog entries from probe results
    _refresh_variables_from_probe(fmu_model, variable_metas)

    # Mark as approved
    probe_record.status = FMUProbeStatus.SUCCEEDED
    probe_record.last_error = ""
    probe_record.details = {"variable_count": len(variables)}
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
        messages=[f"Parsed {len(variables)} variables from modelDescription.xml"],
    )


def _refresh_variables_from_probe(
    fmu_model: FMUModel,
    variables: list,
) -> None:
    """
    Update FMU variable rows and refresh catalog entries based on probe output.

    We rebuild variables from the probe response to make sure the catalog stays
    aligned with the latest FMU metadata.
    """

    validator = fmu_model.validators.first()
    if validator is None:
        return
    FMUVariable.objects.filter(fmu_model=fmu_model).delete()
    entries = ValidatorCatalogEntry.objects.filter(validator=validator)
    entries.delete()
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
