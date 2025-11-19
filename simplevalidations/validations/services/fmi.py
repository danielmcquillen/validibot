from __future__ import annotations

import io
import zipfile
from typing import Iterable

from defusedxml import ElementTree as ET
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from slugify import slugify

from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.submissions.constants import SubmissionDataFormat
from simplevalidations.users.models import Organization
from simplevalidations.validations.constants import (
    CatalogEntryType,
    CatalogRunStage,
    CatalogValueType,
    FMUProbeStatus,
    ValidationType,
)
from simplevalidations.validations.models import (
    FMUModel,
    FMUProbeResult,
    FMIVariable,
    Validator,
    ValidatorCatalogEntry,
)


class FMIIntrospectionError(ValueError):
    """Raised when an FMU cannot be parsed or introspected."""


class FMUStorageError(ValueError):
    """Raised when FMU files cannot be stored or accessed."""


def _read_model_description(fmu_file: UploadedFile | io.BufferedIOBase) -> str:
    try:
        with zipfile.ZipFile(fmu_file, "r") as archive:
            if "modelDescription.xml" not in archive.namelist():
                raise FMIIntrospectionError("FMU is missing modelDescription.xml.")
            with archive.open("modelDescription.xml") as handle:
                return handle.read().decode("utf-8")
    except zipfile.BadZipFile as exc:
        raise FMIIntrospectionError("FMU is not a valid zip archive.") from exc


def _parse_variables(xml_text: str) -> tuple[str, str, list[FMIVariable]]:
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


def create_fmi_validator(
    *,
    org: Organization,
    project,
    name: str,
    upload: UploadedFile,
    description: str = "",
    approve_immediately: bool = True,
    storage_backend=None,
) -> Validator:
    """
    Create an FMI validator, parse the FMU, and seed catalog entries.
    """

    with transaction.atomic():
        try:
            stored_file = upload if storage_backend is None else storage_backend(upload)
        except Exception as exc:  # pragma: no cover - storage failures are surfaced
            raise FMUStorageError(str(exc)) from exc
        fmu = FMUModel.objects.create(
            org=org,
            project=project,
            name=name,
            description=description,
            file=stored_file,
            size_bytes=getattr(upload, "size", 0) or 0,
        )
        model_description = _read_model_description(fmu.file)
        model_name, fmi_version, variables = _parse_variables(model_description)
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
            status=FMUProbeStatus.SUCCEEDED if approve_immediately else FMUProbeStatus.PENDING,
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
