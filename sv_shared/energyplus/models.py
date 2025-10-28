from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Annotated
from typing import Literal

if TYPE_CHECKING:
    from pathlib import Path

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

# Useful constants
STDOUT_TAIL_CHARS = 4000
LOG_TAIL_LINES = 200

Status = Literal["success", "error"]
InvocationMode = Literal["python_api", "cli"]

NonNegFloat = Annotated[float, Field(ge=0)]
NonNegInt = Annotated[int, Field(ge=0)]


class SimulationOutputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    eplusout_sql: Path | None = None
    eplusout_err: Path | None = None
    eplusout_csv: Path | None = None
    eplusout_eso: Path | None = None


class SimulationMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    electricity_kwh: NonNegFloat | None = None
    natural_gas_kwh: NonNegFloat | None = None
    energy_use_intensity_kwh_m2: NonNegFloat | None = None


class SimulationLogs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    err_tail: str | None = None


class SimulationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    simulation_id: str

    outputs: SimulationOutputs = Field(default_factory=SimulationOutputs)
    metrics: SimulationMetrics = Field(default_factory=SimulationMetrics)
    logs: SimulationLogs | None = None

    status: Status = "error"
    weather_file: Path | None = None
    epjson_path: Path | None = None
    output_dir: Path | None = None

    errors: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)

    energyplus_returncode: int = -1
    execution_seconds: NonNegFloat = 0.0
    invocation_mode: InvocationMode = "cli"

    @model_validator(mode="after")
    def _status_consistency(self):
        # If success, expect returncode 0 and typically no errors.
        if self.status == "success":
            if self.energyplus_returncode != 0:
                raise ValueError("success results must have energyplus_returncode == 0")
            if self.errors:
                # allow warnings in messages, but errors list should be empty on success
                raise ValueError("success results must not contain errors")
        elif self.energyplus_returncode == 0:
            raise ValueError("error results should have a nonzero return code")
        return self

    @classmethod
    def bootstrap(
        cls,
        simulation_id: str,
        *,
        output_dir: Path | None = None,
        weather_file: Path | None = None,
        epjson_path: Path | None = None,
        invocation_mode: InvocationMode = "cli",
        include_logs: bool = True,
    ) -> SimulationResult:
        return cls(
            simulation_id=simulation_id,
            status="error",
            weather_file=weather_file,
            epjson_path=epjson_path,
            output_dir=output_dir,
            logs=SimulationLogs() if include_logs else None,
            invocation_mode=invocation_mode,
        )
