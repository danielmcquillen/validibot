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
from vb_shared.fmi import FMIProbeResult

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import FMUProbeStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import FMIVariable
from validibot.validations.models import FMUModel
from validibot.validations.models import FMUProbeResult
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


class FMIIntrospectionError(ValueError):
    """Raised when an FMU cannot be parsed or introspected."""


class FMUStorageError(ValueError):
    """Raised when FMU files cannot be stored or accessed."""


class _FMIProbeRunner:
    """
    FMI probe runner placeholder.

    TODO: Phase 4b - Implement FMI probing via Cloud Run Jobs.
    For now, this is a stub that will raise not-implemented errors.
    """

    @classmethod
    def _invoke_modal_runner(cls, **kwargs):
        """Stub that raises not-implemented error."""
        msg = "FMI probing via Cloud Run Jobs is not yet implemented (Phase 4b)"
        raise NotImplementedError(msg)


def _parse_variables(xml_text: str) -> tuple[str, str, list[FMIVariable]]:
    """Load ScalarVariable entries from modelDescription.xml into FMIVariable shells."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FMIIntrospectionError("Unable to parse modelDescription.xml.") from exc

    fmi_version = root.attrib.get("fmiVersion", "2.0")
    model_name = root.attrib.get("modelName", "fmu")
    ns_prefix = root.tag.split("}")[0].strip("{") if "}" in root.tag else ""
    tag_name = f"{{{ns_prefix}}}ScalarVariable" if ns_prefix else "ScalarVariable"

    variables: list[FMIVariable] = []
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
            FMIVariable(
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
) -> tuple[str, str, list[FMIVariable], str]:
    """
    Perform structural, safety, and metadata validation on an FMU payload.

    Returns ``(model_name, fmi_version, variables, checksum)`` after verifying
    the archive is a ZIP, contains modelDescription.xml, and does not include
    obviously dangerous file types.
    """

    display_name = filename or "fmu"
    if len(payload) > MAX_FMU_SIZE_BYTES:
        raise FMIIntrospectionError(
            _("FMU %(name)s exceeds the maximum size of %(limit)s bytes.")
            % {"name": display_name, "limit": MAX_FMU_SIZE_BYTES},
        )
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            names = archive.namelist()
            if "modelDescription.xml" not in names:
                raise FMIIntrospectionError(
                    _("FMU %(name)s is missing modelDescription.xml.")
                    % {"name": display_name},
                )
            for name in names:
                member = name.lower()
                suffix = Path(member).suffix
                if member.startswith(("../", "/")):
                    raise FMIIntrospectionError(
                        _("FMU %(name)s contains unsafe path entries.")
                        % {"name": display_name},
                    )
                if suffix in DISALLOWED_EXTENSIONS:
                    raise FMIIntrospectionError(
                        _("FMU %(name)s contains disallowed file %(file)s.")
                        % {"name": display_name, "file": name},
                    )
            with archive.open("modelDescription.xml") as handle:
                xml_text = handle.read().decode("utf-8")
    except zipfile.BadZipFile as exc:
        raise FMIIntrospectionError(
            _("FMU %(name)s is not a valid zip archive.") % {"name": display_name}
        ) from exc
    except UnicodeDecodeError as exc:
        raise FMIIntrospectionError(
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


def _should_use_gcs_storage() -> bool:
    """
    Determine whether FMUs should be uploaded to GCS (cloud) or stored locally.

    Local/dev runs should keep files on the filesystem; production/staging
    enables GCS by configuring GCS_VALIDATION_BUCKET (and storages).
    """

    return bool(settings.GCS_VALIDATION_BUCKET)


def _upload_fmu_to_gcs(checksum: str, payload: bytes) -> str:
    """
    Upload a validated FMU payload to GCS and return its gs:// URI.

    Uses checksum-based naming to deduplicate identical FMUs.
    """

    from google.cloud import storage

    bucket_name = settings.GCS_VALIDATION_BUCKET
    if not bucket_name:
        msg = "GCS_VALIDATION_BUCKET is not configured."
        raise FMUStorageError(msg)

    object_path = f"fmus/{checksum}.fmu"
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_path)
    blob.upload_from_string(payload, content_type="application/octet-stream")
    return f"gs://{bucket_name}/{object_path}"


def create_fmi_validator(
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
    Create an FMI validator, parse the FMU, and seed catalog entries.

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
            gcs_uri=_upload_fmu_to_gcs(checksum, raw_bytes)
            if _should_use_gcs_storage()
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
        fmu.fmi_version = fmi_version
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
            validation_type=ValidationType.FMI,
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
    variables: Iterable[FMIVariable],
) -> None:
    prepared: list[FMIVariable] = []
    for var in variables:
        var.fmu_model = fmu_model
        prepared.append(var)
    FMIVariable.objects.bulk_create(prepared)
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
            target_field=var.name,
            input_binding_path="",
            data_type=_data_type_for_variable(var.value_type),
            metadata={"fmi_value_type": var.value_type, "unit": var.unit},
            is_required=(run_stage == CatalogRunStage.INPUT),
        )
        FMIVariable.objects.filter(
            fmu_model=fmu_model,
            name=var.name,
        ).update(catalog_entry=entry)


def run_fmu_probe(fmu_model: FMUModel, *, return_logs: bool = False) -> FMIProbeResult:
    """
    Invoke the Modal probe function for an FMU and update metadata + catalog.

    A probe is a short, safety-first run that parses modelDescription.xml and
    confirms the FMU can be opened. We use it to populate variables and mark the
    FMU as approved before allowing workflow authors to attach assertions.
    """

    runner = _FMIProbeRunner()
    storage_key = (
        fmu_model.gcs_uri
        or getattr(fmu_model.file, "path", "")
        or getattr(fmu_model.file, "name", "")
    )
    fmu_url = fmu_model.gcs_uri or getattr(fmu_model.file, "url", None)
    probe_record, _ = FMUProbeResult.objects.get_or_create(
        fmu_model=fmu_model,
        defaults={"status": FMUProbeStatus.PENDING},
    )
    probe_record.status = FMUProbeStatus.RUNNING
    probe_record.last_error = ""
    probe_record.save(update_fields=["status", "last_error", "modified"])

    try:
        raw = runner._invoke_modal_runner(  # noqa: SLF001
            fmu_storage_key=storage_key,
            fmu_url=fmu_url,
            fmu_checksum=fmu_model.checksum,
            return_logs=return_logs,
        )
        result = FMIProbeResult.model_validate(raw)
    except Exception as exc:  # pragma: no cover - defensive
        probe_record.mark_failed(str(exc))
        return FMIProbeResult.failure(errors=[str(exc)])

    if result.status == "success":
        _refresh_variables_from_probe(fmu_model, result.variables)
        probe_record.status = FMUProbeStatus.SUCCEEDED
        probe_record.last_error = ""
        probe_record.details = {"variable_count": len(result.variables)}
        fmu_model.is_approved = True
    else:
        probe_record.status = FMUProbeStatus.FAILED
        probe_record.last_error = "; ".join(result.errors or [])
        fmu_model.is_approved = False

    probe_record.save(update_fields=["status", "last_error", "details", "modified"])
    fmu_model.save(update_fields=["is_approved", "modified"])
    return result


def _refresh_variables_from_probe(
    fmu_model: FMUModel,
    variables: list,
) -> None:
    """
    Update FMIVariable rows and refresh catalog entries based on probe output.

    We rebuild variables from the probe response to make sure the catalog stays
    aligned with the latest FMU metadata.
    """

    validator = fmu_model.validators.first()
    if validator is None:
        return
    FMIVariable.objects.filter(fmu_model=fmu_model).delete()
    entries = ValidatorCatalogEntry.objects.filter(validator=validator)
    entries.delete()
    shaped_vars = [
        FMIVariable(
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
