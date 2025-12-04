"""
EnergyPlus validator container entrypoint.

This Cloud Run Job container:
1. Downloads input.json from GCS
2. Downloads input files (IDF, EPW) from GCS
3. Runs EnergyPlus simulation
4. Uploads output.json to GCS
5. POSTs callback to Django
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC
from datetime import datetime

# Import runner after shared utilities
from runner import run_energyplus_simulation
from sv_shared.energyplus.envelopes import EnergyPlusInputEnvelope
from sv_shared.energyplus.envelopes import EnergyPlusOutputEnvelope
from sv_shared.energyplus.envelopes import EnergyPlusOutputs
from sv_shared.validations.envelopes import ValidationStatus

from validators.shared.callback_client import post_callback
from validators.shared.envelope_loader import get_output_uri
from validators.shared.envelope_loader import load_input_envelope
from validators.shared.gcs_client import upload_envelope

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def main() -> int:
    """
    Main entrypoint for EnergyPlus validator container.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    started_at = datetime.now(UTC)

    try:
        # Load input envelope from GCS
        logger.info("Loading input envelope...")
        input_envelope = load_input_envelope(EnergyPlusInputEnvelope)

        logger.info(
            "Loaded input envelope for run_id=%s, validator=%s v%s",
            input_envelope.run_id,
            input_envelope.validator.type,
            input_envelope.validator.version,
        )

        # Run EnergyPlus simulation
        logger.info("Running EnergyPlus simulation...")
        outputs = run_energyplus_simulation(input_envelope)

        # Determine status from simulation results
        if outputs.energyplus_returncode == 0:
            status = ValidationStatus.SUCCESS
        else:
            status = ValidationStatus.FAILED_VALIDATION

        finished_at = datetime.now(UTC)

        # Create output envelope
        logger.info("Creating output envelope...")
        output_envelope = EnergyPlusOutputEnvelope(
            run_id=input_envelope.run_id,
            validator=input_envelope.validator,
            status=status,
            timing={
                "started_at": started_at,
                "finished_at": finished_at,
            },
            messages=[],  # Populated by runner if needed
            metrics=[],  # Populated by runner if needed
            artifacts=[],  # Populated by runner if needed
            outputs=outputs,
        )

        # Upload output envelope to GCS
        output_uri = get_output_uri(input_envelope)
        logger.info("Uploading output envelope to %s", output_uri)
        upload_envelope(output_envelope, output_uri)

        # POST callback to Django
        logger.info("Sending callback to Django...")
        post_callback(
            callback_url=str(input_envelope.context.callback_url),
            callback_token=input_envelope.context.callback_token,
            run_id=input_envelope.run_id,
            status=status,
            result_uri=output_uri,
        )

        logger.info("Validation complete (status=%s)", status.value)
        return 0  # noqa: TRY300

    except Exception:
        logger.exception("Validation failed with unexpected error")

        # Try to POST failure callback if we have input envelope
        try:
            if "input_envelope" in locals():
                finished_at = datetime.now(UTC)

                # Create minimal failure envelope
                failure_envelope = EnergyPlusOutputEnvelope(
                    run_id=input_envelope.run_id,
                    validator=input_envelope.validator,
                    status=ValidationStatus.FAILED_RUNTIME,
                    timing={
                        "started_at": started_at,
                        "finished_at": finished_at,
                    },
                    messages=[
                        {
                            "severity": "ERROR",
                            "text": "Validator container failed with runtime error",
                        }
                    ],
                    outputs=EnergyPlusOutputs(
                        energyplus_returncode=-1,
                        execution_seconds=0,
                        invocation_mode="cli",
                    ),
                )

                output_uri = get_output_uri(input_envelope)
                upload_envelope(failure_envelope, output_uri)

                post_callback(
                    callback_url=str(input_envelope.context.callback_url),
                    callback_token=input_envelope.context.callback_token,
                    run_id=input_envelope.run_id,
                    status=ValidationStatus.FAILED_RUNTIME,
                    result_uri=output_uri,
                )
        except Exception:
            logger.exception("Failed to send failure callback")

        return 1


if __name__ == "__main__":
    sys.exit(main())
