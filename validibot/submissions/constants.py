from django.db import models
from django.utils.translation import gettext_lazy as _


class DataRetention(models.TextChoices):
    """
    Retention policy for submission data.

    Controls how long user-submitted content is stored before being purged.
    The actual record is preserved for audit trail; only the content is removed.
    """

    DO_NOT_STORE = "DO_NOT_STORE", _("Do not store (delete after validation)")
    STORE_10_DAYS = "STORE_10_DAYS", _("Store for 10 days")
    STORE_30_DAYS = "STORE_30_DAYS", _("Store for 30 days")


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
