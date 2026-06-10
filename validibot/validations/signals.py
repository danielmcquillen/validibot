"""
Django signals for the validations app.

These signals allow external packages (e.g., validibot-cloud) to react to
validation lifecycle events without the community code needing to know about
them. This follows the standard Django decoupling pattern used throughout
the project (see users/signals.py, tracking/signals.py).

Receivers should be connected in the consuming app's AppConfig.ready() method.
"""

from django.dispatch import Signal

# Fired after a validation run is successfully created.
# Provides: validation_run (ValidationRun), workflow_type (str)
validation_run_created = Signal()

# Fired after a validation step completes via callback (advanced validators).
# Provides:
#   step_run (ValidationStepRun)
#   validation_run (ValidationRun)
#   envelope_status (str): the container envelope's ValidationStatus value
#       ("success" | "failed_validation" | "failed_runtime" | "cancelled").
#   ran_to_completion (bool): True when the container actually executed and
#       produced a result (envelope SUCCESS or FAILED_VALIDATION — "finished but
#       had errors"); False for runtime failures / cancellation. Metering uses
#       this to charge compute only for runs that ran to completion.
validation_step_completed = Signal()

# Fired after a validation run reaches a terminal status (SUCCEEDED, FAILED,
# CANCELED, TIMED_OUT) on either the sync (orchestrator) or async (callback)
# finalization path. Cloud metering uses this to RELEASE any compute-credit
# reservation held for the run at launch. A periodic reaper is the backstop for
# runs that reach a terminal state without passing one of those two finalize
# sites (e.g. a lost callback, or the launch-time enqueue failure path), so a
# missed emission only delays release by one reaper cycle — it never leaks a
# hold.
# Provides: validation_run (ValidationRun)
validation_run_finalized = Signal()
