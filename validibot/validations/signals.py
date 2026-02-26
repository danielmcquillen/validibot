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
# Provides: step_run (ValidationStepRun), validation_run (ValidationRun)
validation_step_completed = Signal()
