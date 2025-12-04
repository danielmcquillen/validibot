from __future__ import annotations

import hashlib
import io
import os
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from defusedxml import ElementTree as ET  # noqa: N817
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from slugify import slugify
from sv_shared.fmi import FMIProbeResult

from simplevalidations.submissions.constants import SubmissionDataFormat
from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.validations.constants import CatalogEntryType
from simplevalidations.validations.constants import CatalogRunStage
from simplevalidations.validations.constants import CatalogValueType
from simplevalidations.validations.constants import FMUProbeStatus
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.models import FMIVariable
from simplevalidations.validations.models import FMUModel
from simplevalidations.validations.models import FMUProbeResult
from simplevalidations.validations.models import Validator
from simplevalidations.validations.models import ValidatorCatalogEntry

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.core.files.uploadedfile import UploadedFile

    from simplevalidations.users.models import Organization


MAX_FMU_SIZE_BYTES = 50 * 1024 * 1024
DISALLOWED_EXTENSIONS = {
    ".exe",
    ".bat",
    ".sh",
    ".cmd",
}
_TRUE_STRINGS = {"1", "true", "yes", "on"}


class FMIIntrospectionError(ValueError):
    """Raised when an FMU cannot be parsed or introspected."""


class FMUStorageError(ValueError):
    """Raised when FMU files cannot be stored or accessed."""


class _FMIProbeRunner:
    """
    FMI probe runner placeholder.

    TODO: Phase 4 - Implement FMI probing via Cloud Run Jobs.
    For now, this is a stub that will raise not-implemented errors.
    """

    @classmethod
    def configure_modal_runner(cls, mock_callable, *, cleanup_callable=None):
        """Stub for backward compatibility with tests."""
        pass  # noqa: PIE790

    @classmethod
    def _invoke_modal_runner(cls, **kwargs):
        """Stub that raises not-implemented error."""
        msg = "FMI probing via Cloud Run Jobs is not yet implemented (Phase 4)"
        raise NotImplementedError(msg)


def _use_test_volume() -> bool:
    """
    Return True when the environment requests the test Modal Volume.

    This keeps production and test FMUs isolated in separate Modal volumes.
    """

    raw = os.getenv("FMI_USE_TEST_VOLUME", "")
    return str(raw).lower() in _TRUE_STRINGS


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


def _cache_fmu_in_modal_volume(
    *,
    fmu_model: FMUModel,
    checksum: str,
    filename: str,
    payload: bytes,
    use_test_volume: bool = False,
) -> str:
    """
    Copy a validated FMU into the Modal Volume cache using the Modal client.

    This avoids presigned URLs and keeps uploads on the control plane; the
    Modal runtime reads directly from the cached FMU keyed by checksum.
    """

    return _upload_to_modal_volume(
        checksum=checksum,
        filename=filename,
        payload=payload,
        use_test_volume=use_test_volume,
    )


def _upload_to_modal_volume(
    *,
    checksum: str,
    filename: str,
    payload: bytes,
    use_test_volume: bool,
) -> str:
    """
    Write an FMU into the appropriate Modal Volume from the control plane.

    In production we write to the production volume; for tests we route to a
    dedicated test volume. The returned path matches the mount points used in
    the Modal runtime (/fmus or /fmus-test). This helper assumes control-plane
    credentials (env vars or ~/.modal.toml) are available.
    """

    try:
        import modal
    except ImportError as exc:  # pragma: no cover - environment dependency
        raise FMUStorageError("Install 'modal' to cache FMUs in Modal Volume.") from exc

    volume_name = (
        os.getenv("FMI_TEST_VOLUME_NAME", "fmi-cache-test")
        if use_test_volume
        else os.getenv("FMI_VOLUME_NAME", "fmi-cache")
    )
    mount_prefix = "/fmus-test" if use_test_volume else "/fmus"
    remote_name = f"{checksum}.fmu"

    volume = modal.Volume.from_name(volume_name, create_if_missing=True)
    if hasattr(volume, "batch_upload"):
        with tempfile.NamedTemporaryFile(
            prefix="fmu-upload-", suffix=".fmu", delete=False
        ) as tmp:
            tmp.write(payload)
            tmp.flush()
            with volume.batch_upload(force=True) as batch:  # type: ignore[arg-type]
                batch.put_file(tmp.name, f"/{remote_name}")
    elif hasattr(volume, "put_file"):
        # Older client without batch_upload but with put_file
        with tempfile.NamedTemporaryFile(
            prefix="fmu-upload-", suffix=".fmu", delete=False
        ) as tmp:
            tmp.write(payload)
            tmp.flush()
            volume.put_file(tmp.name, f"/{remote_name}")
    elif hasattr(volume, "__setitem__"):
        # Fallback for legacy clients: direct bytes assignment into the volume mapping.
        volume[remote_name] = payload  # type: ignore[index]
    else:  # pragma: no cover - defensive against unexpected client versions
        raise FMUStorageError(
            "Modal Volume API missing batch_upload/put_file/item assignment."
        )
    return f"{mount_prefix}/{remote_name}"


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
        try:
            stored_file = (
                wrapped_upload
                if storage_backend is None
                else storage_backend(wrapped_upload)
            )
        except Exception as exc:  # pragma: no cover - storage failures are surfaced
            raise FMUStorageError(str(exc)) from exc
        fmu = FMUModel.objects.create(
            org=org,
            project=project,
            name=name,
            description=description,
            file=stored_file,
            size_bytes=len(raw_bytes),
            checksum=checksum,
        )
        fmu.fmi_version = fmi_version
        fmu.introspection_metadata = {
            "model_name": model_name,
            "variable_count": len(variables),
        }
        fmu.is_approved = approve_immediately
        fmu.save()

        try:
            fmu.modal_volume_path = _cache_fmu_in_modal_volume(
                fmu_model=fmu,
                checksum=checksum,
                filename=upload.name,
                payload=raw_bytes,
                use_test_volume=_use_test_volume(),
            )
        except Exception as exc:  # pragma: no cover - defensive
            raise FMUStorageError(str(exc)) from exc
        fmu.save(update_fields=["modal_volume_path", "modified"])

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
    storage_key = getattr(fmu_model.file, "path", "") or getattr(
        fmu_model.file,
        "name",
        "",
    )
    fmu_url = getattr(fmu_model.file, "url", None)
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
            use_test_volume=_use_test_volume(),
            return_logs=return_logs,
        )
        result = FMIProbeResult.model_validate(raw)
    except Exception as exc:  # pragma: no cover - defensive
        probe_record.mark_failed(str(exc))
        raise

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
