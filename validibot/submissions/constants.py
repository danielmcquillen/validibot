from datetime import timedelta

from django.db import models
from django.utils.translation import gettext_lazy as _


class SubmissionRetention(models.TextChoices):
    """
    Retention policy for user-submitted files.

    Controls how long user-submitted content is stored before being purged.
    The actual submission record is preserved for audit trail; only the
    content (file/inline data) is removed.

    Options:
        DO_NOT_STORE: Delete immediately after validation completes successfully.
                      This is the most privacy-respecting option.
        STORE_1_DAY: Keep for 24 hours (for quick re-downloads).
        STORE_7_DAYS: Keep for 1 week (default, good balance).
        STORE_30_DAYS: Keep for 1 month (for longer review cycles).
        STORE_PERMANENTLY: Never auto-delete (manual deletion only).
    """

    DO_NOT_STORE = "DO_NOT_STORE", _("Do not store (delete after validation)")
    STORE_1_DAY = "STORE_1_DAY", _("Store for 1 day")
    STORE_7_DAYS = "STORE_7_DAYS", _("Store for 7 days")
    STORE_30_DAYS = "STORE_30_DAYS", _("Store for 30 days")
    STORE_PERMANENTLY = "STORE_PERMANENTLY", _("Store permanently")


# Backwards compatibility alias
DataRetention = SubmissionRetention


class OutputRetention(models.TextChoices):
    """
    Retention policy for validator outputs, artifacts, and findings.

    Controls how long validation results are stored before being purged.
    Unlike submission retention, outputs cannot be set to DO_NOT_STORE
    because users need time to download/review results.

    Options:
        STORE_7_DAYS: Keep for 1 week (minimum for users to review).
        STORE_30_DAYS: Keep for 1 month (default, good for most use cases).
        STORE_90_DAYS: Keep for 3 months (for audit/compliance needs).
        STORE_1_YEAR: Keep for 1 year (for long-term records).
        STORE_PERMANENTLY: Never auto-delete (manual deletion only).
    """

    STORE_7_DAYS = "STORE_7_DAYS", _("Store for 7 days")
    STORE_30_DAYS = "STORE_30_DAYS", _("Store for 30 days")
    STORE_90_DAYS = "STORE_90_DAYS", _("Store for 90 days")
    STORE_1_YEAR = "STORE_1_YEAR", _("Store for 1 year")
    STORE_PERMANENTLY = "STORE_PERMANENTLY", _("Store permanently")


# Mapping from retention policy to timedelta (None means never expires)
SUBMISSION_RETENTION_DAYS: dict[str, int | None] = {
    SubmissionRetention.DO_NOT_STORE: 0,  # Immediate deletion
    SubmissionRetention.STORE_1_DAY: 1,
    SubmissionRetention.STORE_7_DAYS: 7,
    SubmissionRetention.STORE_30_DAYS: 30,
    SubmissionRetention.STORE_PERMANENTLY: None,  # Never expires
}


OUTPUT_RETENTION_DAYS: dict[str, int | None] = {
    OutputRetention.STORE_7_DAYS: 7,
    OutputRetention.STORE_30_DAYS: 30,
    OutputRetention.STORE_90_DAYS: 90,
    OutputRetention.STORE_1_YEAR: 365,
    OutputRetention.STORE_PERMANENTLY: None,  # Never expires
}


def get_submission_retention_timedelta(policy: str) -> timedelta | None:
    """
    Get the timedelta for a submission retention policy.

    Args:
        policy: The retention policy value (e.g., SubmissionRetention.STORE_7_DAYS)

    Returns:
        timedelta for the policy, or None if the policy is STORE_PERMANENTLY.
        Returns timedelta(0) for DO_NOT_STORE (immediate deletion).
    """
    days = SUBMISSION_RETENTION_DAYS.get(policy)
    if days is None:
        return None
    return timedelta(days=days)


def get_output_retention_timedelta(policy: str) -> timedelta | None:
    """
    Get the timedelta for an output retention policy.

    Args:
        policy: The retention policy value (e.g., OutputRetention.STORE_30_DAYS)

    Returns:
        timedelta for the policy, or None if the policy is STORE_PERMANENTLY.
    """
    days = OUTPUT_RETENTION_DAYS.get(policy)
    if days is None:
        return None
    return timedelta(days=days)


class SubmissionFileType(models.TextChoices):
    """
    The file type of the initial submission sent
    via API or web form. This is the raw form of the submission,
    and not really what the data respresents. (See SubmissionDataFormat
    for that.)
    """

    JSON = "json", _("JSON")
    XML = "xml", _("XML")
    TEXT = "text", _("Plain Text")
    YAML = "yaml", _("YAML")
    BINARY = "binary", _("Binary")
    UNKNOWN = "UNKNOWN", _("Unknown")


class SubmissionDataFormat(models.TextChoices):
    """
    The data format that the submission represents.
    This is distinct from the file type, as a submission
    file could be in one format but represent data in another format.

    For example, an EnergyPlus IDF file submission would be a
    SubmissionFileType.TEXT file_type, but the data_format would be
    SubmissionDataFormat.ENERGYPLUS_IDF.

    So in essense, SubmissionDataFormat describes the domain-specific format
    of the data contained within the submission file.

    """

    JSON = "json", _("JSON")
    XML = "xml", _("XML")
    TEXT = "text", _("Plain Text")
    YAML = "yaml", _("YAML")
    ENERGYPLUS_IDF = "energyplus_idf", _("EnergyPlus IDF")
    ENERGYPLUS_EPJSON = "energyplus_epjson", _("EnergyPlus epJSON")
    FMU = "fmu", _("FMU")
    UNKNOWN = "unknown", _("Unknown")


# Map known data formats to the submission file types that can carry them.
DATA_FORMAT_FILE_TYPE_MAP: dict[str, list[str]] = {
    SubmissionDataFormat.JSON: [SubmissionFileType.JSON, SubmissionFileType.TEXT],
    SubmissionDataFormat.XML: [SubmissionFileType.XML, SubmissionFileType.TEXT],
    SubmissionDataFormat.TEXT: [SubmissionFileType.TEXT],
    SubmissionDataFormat.YAML: [SubmissionFileType.YAML, SubmissionFileType.TEXT],
    SubmissionDataFormat.ENERGYPLUS_IDF: [SubmissionFileType.TEXT],
    SubmissionDataFormat.ENERGYPLUS_EPJSON: [
        SubmissionFileType.JSON,
        SubmissionFileType.TEXT,
    ],
    SubmissionDataFormat.FMU: [SubmissionFileType.BINARY],
    SubmissionDataFormat.UNKNOWN: [SubmissionFileType.UNKNOWN],
}


def data_format_allowed_file_types(data_format: str) -> list[str]:
    """
    Return the submission file types that can carry the given data format.

    Fall back to an empty list when the format is unknown so callers can
    decide how to handle unrecognized formats.
    """
    return list(DATA_FORMAT_FILE_TYPE_MAP.get(data_format, []))
