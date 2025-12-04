"""
EnergyPlus simulation runner.

Handles downloading input files, running EnergyPlus, and extracting results.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from sv_shared.energyplus.envelopes import EnergyPlusOutputs
from sv_shared.energyplus.models import EnergyPlusSimulationLogs
from sv_shared.energyplus.models import EnergyPlusSimulationMetrics
from sv_shared.energyplus.models import EnergyPlusSimulationOutputs
from sv_shared.energyplus.models import STDOUT_TAIL_CHARS

from validators.shared.gcs_client import download_file

if TYPE_CHECKING:
    from sv_shared.energyplus.envelopes import EnergyPlusInputEnvelope

logger = logging.getLogger(__name__)


def run_energyplus_simulation(input_envelope: EnergyPlusInputEnvelope) -> EnergyPlusOutputs:
    """
    Run EnergyPlus simulation based on input envelope.

    Args:
        input_envelope: Typed input envelope with files and configuration

    Returns:
        EnergyPlus outputs including returncode, metrics, logs, and file paths

    Raises:
        ValueError: If required input files are missing
        RuntimeError: If EnergyPlus execution fails critically
    """
    start_time = time.time()

    # Create working directory
    work_dir = Path("/tmp/energyplus_run") / input_envelope.run_id
    work_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Working directory: %s", work_dir)

    # Download input files from GCS
    logger.info("Downloading input files...")
    model_file = _download_input_files(input_envelope, work_dir)

    # Run EnergyPlus
    logger.info("Running EnergyPlus simulation...")
    returncode, stdout, stderr = _run_energyplus(
        model_file=model_file,
        work_dir=work_dir,
        config=input_envelope.inputs,
    )

    execution_seconds = time.time() - start_time

    # Collect output file paths
    outputs = EnergyPlusSimulationOutputs(
        eplusout_sql=work_dir / "eplusout.sql" if (work_dir / "eplusout.sql").exists() else None,
        eplusout_err=work_dir / "eplusout.err" if (work_dir / "eplusout.err").exists() else None,
        eplusout_csv=work_dir / "eplusout.csv" if (work_dir / "eplusout.csv").exists() else None,
        eplusout_eso=work_dir / "eplusout.eso" if (work_dir / "eplusout.eso").exists() else None,
    )

    # Extract metrics from SQL database
    logger.info("Extracting metrics...")
    metrics = _extract_metrics(outputs.eplusout_sql)

    # Collect logs
    logs = EnergyPlusSimulationLogs(
        stdout_tail=stdout[-STDOUT_TAIL_CHARS:] if stdout else None,
        stderr_tail=stderr[-STDOUT_TAIL_CHARS:] if stderr else None,
        err_tail=_read_err_tail(outputs.eplusout_err),
    )

    logger.info(
        "Simulation complete (returncode=%d, duration=%.2fs)",
        returncode,
        execution_seconds,
    )

    return EnergyPlusOutputs(
        outputs=outputs,
        metrics=metrics,
        logs=logs,
        energyplus_returncode=returncode,
        execution_seconds=execution_seconds,
        invocation_mode=input_envelope.inputs.invocation_mode,
    )


def _download_input_files(
    input_envelope: EnergyPlusInputEnvelope,
    work_dir: Path,
) -> Path:
    """
    Download input files from GCS to working directory.

    Args:
        input_envelope: Input envelope with file URIs
        work_dir: Local working directory

    Returns:
        Path to the primary model file (IDF or epJSON)

    Raises:
        ValueError: If primary model file is missing
    """
    model_file = None

    for file_item in input_envelope.input_files:
        logger.info("Downloading %s (role=%s)", file_item.name, file_item.role)

        destination = work_dir / file_item.name
        download_file(file_item.uri, destination)

        # Track primary model file
        if file_item.role == "primary-model":
            model_file = destination

    if model_file is None:
        raise ValueError("No primary-model file found in input_files")

    return model_file


def _run_energyplus(
    model_file: Path,
    work_dir: Path,
    config,
) -> tuple[int, str, str]:
    """
    Execute EnergyPlus simulation.

    Args:
        model_file: Path to IDF or epJSON model file
        work_dir: Working directory for simulation
        config: EnergyPlusInputs configuration

    Returns:
        Tuple of (returncode, stdout, stderr)
    """
    # Find weather file (should be in work_dir)
    weather_file = None
    for f in work_dir.glob("*.epw"):
        weather_file = f
        break

    if weather_file is None:
        logger.warning("No weather file found, EnergyPlus may fail")

    # Build EnergyPlus command
    cmd = [
        "energyplus",
        "--output-directory", str(work_dir),
    ]

    if weather_file:
        cmd.extend(["--weather", str(weather_file)])

    cmd.append(str(model_file))

    logger.info("Executing: %s", " ".join(cmd))

    # Run EnergyPlus
    result = subprocess.run(
        cmd,
        cwd=work_dir,
        capture_output=True,
        text=True,
        timeout=3600,  # 1 hour timeout
    )

    logger.info("EnergyPlus returncode: %d", result.returncode)

    return result.returncode, result.stdout, result.stderr


def _extract_metrics(sql_path: Path | None) -> EnergyPlusSimulationMetrics:
    """
    Extract metrics from EnergyPlus SQL output database.

    Args:
        sql_path: Path to eplusout.sql file (or None if not generated)

    Returns:
        Extracted metrics (may be empty if SQL is missing)
    """
    if sql_path is None or not sql_path.exists():
        logger.warning("No SQL database found, cannot extract metrics")
        return EnergyPlusSimulationMetrics()

    # TODO: Implement SQL metric extraction
    # This is a placeholder - you'll need to:
    # 1. Connect to SQLite database
    # 2. Query for electricity, gas, EUI values
    # 3. Return populated EnergyPlusSimulationMetrics
    #
    # For now, return empty metrics
    logger.warning("Metric extraction not yet implemented")
    return EnergyPlusSimulationMetrics()


def _read_err_tail(err_path: Path | None, max_lines: int = 200) -> str | None:
    """
    Read tail of eplusout.err file.

    Args:
        err_path: Path to eplusout.err file (or None if not generated)
        max_lines: Maximum number of lines to read from end of file

    Returns:
        Tail of err file, or None if file doesn't exist
    """
    if err_path is None or not err_path.exists():
        return None

    try:
        with err_path.open("r") as f:
            lines = f.readlines()
            tail_lines = lines[-max_lines:] if len(lines) > max_lines else lines
            return "".join(tail_lines)
    except Exception as e:
        logger.warning("Failed to read err file: %s", e)
        return None
