"""
Tests for artifact-port contract validation.

Artifact ports are the workflow-facing file/data contracts used during launch.
These tests keep the reusable contract service honest without coupling every
case to Cloud Run envelope assembly or trace persistence.
"""

from dataclasses import dataclass
from typing import Any

import pytest
from validibot_shared.validations.envelopes import InputFileItem
from validibot_shared.validations.envelopes import ResourceFileItem
from validibot_shared.validations.envelopes import SupportedMimeType

from validibot.submissions.constants import SubmissionDataFormat
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import ResourceFileType
from validibot.validations.services import artifact_ports


@dataclass
class ArtifactPort:
    """Small in-memory port shape for pure contract tests."""

    contract_key: str
    role: str | None = None
    resource_type: str | None = None
    data_format: str | None = None
    media_type: str | None = None
    accepted_data_formats: list[str] | None = None
    accepted_media_types: list[str] | None = None
    allowed_source_scopes: list[str] | None = None
    metadata: dict[str, Any] | None = None
    min_items: int | None = None
    max_items: int | None = None


def _primary_model_port(**overrides) -> ArtifactPort:
    """Return an EnergyPlus model file port with production-like defaults."""

    values = {
        "contract_key": "primary_model",
        "role": "primary-model",
        "data_format": SubmissionDataFormat.ENERGYPLUS_IDF,
        "media_type": SupportedMimeType.ENERGYPLUS_IDF.value,
        "accepted_data_formats": [
            SubmissionDataFormat.ENERGYPLUS_IDF,
            SubmissionDataFormat.ENERGYPLUS_EPJSON,
        ],
        "accepted_media_types": [
            SupportedMimeType.ENERGYPLUS_IDF.value,
            SupportedMimeType.ENERGYPLUS_EPJSON.value,
        ],
        "allowed_source_scopes": [
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
        "metadata": {"accepted_extensions": ["idf", "epjson", "json"]},
        "min_items": 1,
        "max_items": 1,
    }
    values.update(overrides)
    return ArtifactPort(**values)


def _weather_file_port(**overrides) -> ArtifactPort:
    """Return an EnergyPlus weather file port with production-like defaults."""

    values = {
        "contract_key": "weather_file",
        "role": "weather",
        "resource_type": ResourceFileType.ENERGYPLUS_WEATHER,
        "data_format": ResourceFileType.ENERGYPLUS_WEATHER,
        "media_type": SupportedMimeType.ENERGYPLUS_EPW.value,
        "accepted_data_formats": [ResourceFileType.ENERGYPLUS_WEATHER],
        "accepted_media_types": [SupportedMimeType.ENERGYPLUS_EPW.value],
        "allowed_source_scopes": [
            BindingSourceScope.WORKFLOW_RESOURCE,
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
        "metadata": {"accepted_extensions": ["epw"]},
        "min_items": 1,
        "max_items": 1,
    }
    values.update(overrides)
    return ArtifactPort(**values)


class TestArtifactPortContractValidation:
    """Coverage for the reusable artifact-port validation service."""

    def test_input_file_item_accepts_declared_epjson_contract(self):
        """epJSON should pass when the model port declares the format and MIME."""

        artifact_ports.validate_input_file_item(
            port=_primary_model_port(),
            item=InputFileItem(
                name="model.epjson",
                mime_type=SupportedMimeType.ENERGYPLUS_EPJSON,
                role="primary-model",
                port_key="primary_model",
                uri="file:///validibot/input/model.epjson",
            ),
        )

    def test_input_file_item_rejects_unaccepted_media_type(self):
        """MIME validation must be separate from extension/data-format checks."""

        port = _primary_model_port(
            accepted_media_types=[SupportedMimeType.ENERGYPLUS_IDF.value],
        )

        with pytest.raises(ValueError, match="does not accept media type"):
            artifact_ports.validate_input_file_item(
                port=port,
                item=InputFileItem(
                    name="model.epjson",
                    mime_type=SupportedMimeType.ENERGYPLUS_EPJSON,
                    role="primary-model",
                    port_key="primary_model",
                    uri="file:///validibot/input/model.epjson",
                ),
            )

    def test_file_uri_rejects_extensions_outside_the_port_allowlist(self):
        """Extension allowlists should fail before an invalid file reaches a runner."""

        with pytest.raises(ValueError, match=r"expected one of \.epw"):
            artifact_ports.validate_file_uri(
                port=_weather_file_port(),
                uri="file:///validibot/input/weather.txt",
            )

    def test_artifact_ref_accepts_generic_json_for_epjson_uri(self):
        """Legacy upstream artifacts may use JSON MIME for epJSON filenames."""

        artifact_ports.validate_artifact_ref(
            port=_primary_model_port(),
            artifact_ref={
                "uri": "gs://validibot/runs/run-1/model.epjson",
                "data_format": SubmissionDataFormat.ENERGYPLUS_EPJSON,
                "media_type": "application/json",
            },
        )

    def test_artifact_ref_rejects_wrong_explicit_media_type(self):
        """A wrong explicit artifact MIME should not be hidden by the filename."""

        with pytest.raises(ValueError, match="application/pdf"):
            artifact_ports.validate_artifact_ref(
                port=_primary_model_port(),
                artifact_ref={
                    "uri": "gs://validibot/runs/run-1/model.epjson",
                    "data_format": SubmissionDataFormat.ENERGYPLUS_EPJSON,
                    "media_type": "application/pdf",
                },
            )

    def test_resource_item_enforces_resource_type_and_file_contract(self):
        """Workflow resources must match both domain type and file constraints."""

        artifact_ports.validate_resource_file_item(
            port=_weather_file_port(),
            item=ResourceFileItem(
                id="resource-weather-123",
                type=ResourceFileType.ENERGYPLUS_WEATHER,
                port_key="weather_file",
                uri="gs://validibot/resources/weather.epw",
            ),
        )

        with pytest.raises(ValueError, match="expected resource type"):
            artifact_ports.validate_resource_file_item(
                port=_weather_file_port(),
                item=ResourceFileItem(
                    id="resource-library-123",
                    type="library",
                    port_key="weather_file",
                    uri="gs://validibot/resources/library.epw",
                ),
            )

    def test_source_scope_and_cardinality_fail_closed(self):
        """Binding scope and artifact count are part of the same launch contract."""

        port = _primary_model_port(
            allowed_source_scopes=[BindingSourceScope.UPSTREAM_ARTIFACT],
        )

        with pytest.raises(ValueError, match="does not allow source scope"):
            artifact_ports.validate_source_scope(
                port,
                BindingSourceScope.SUBMISSION_FILE,
            )

        with pytest.raises(ValueError, match="expected at least 1"):
            artifact_ports.validate_cardinality(
                port=port,
                count=0,
                source_description="submitted files",
            )
