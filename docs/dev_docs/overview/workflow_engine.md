# Workflow Engine Architecture

This document describes the internal architecture of the `ValidationRunService`, which orchestrates the execution of validation workflows.

## Overview

The **Workflow Engine** is responsible for:
1.  Iterating through `WorkflowStep`s defined in a `Workflow`.
2.  Executing each step against a `Submission`.
3.  Recording the results (`ValidationFinding`, status updates).
4.  Handling dependencies and flow control (future).

## Architecture: Command/Handler Pattern

Previously, the engine was hardcoded to only support `Validator` steps. It has been refactored to use a **Command/Handler Pattern** to support generic Actions (e.g., Slack notifications, Certificate issuance).

### Core Components

#### 1. Dispatcher (`ValidationRunService`)
The service acts as a simple dispatcher. For each step, it:
- Resolves the appropriate implementation (`Validator` or `Action`).
- Looks up the registered `StepHandler`.
- Invokes `handler.execute(context)`.

#### 2. Protocol (`StepHandler`)
All execution logic must implement the `StepHandler` protocol defined in `validibot/actions/protocols.py`:

```python
class StepHandler(Protocol):
    def execute(self, run_context: RunContext) -> StepResult:
        ...
```

- **RunContext**: Contains the `ValidationRun`, `WorkflowStep`, and shared signals.
- **StepResult**: Standardized output indicating pass/fail, issues, and statistics.

### Handlers

#### ValidatorStepHandler
A specialized adapter that wraps the legacy `BaseValidatorEngine`. It allows existing external validators (EnergyPlus, FMI) to running within the new unified engine without modification.

#### Action Handlers
Generic handlers for internal actions.
- **SlackMessageActionHandler**: Sends notifications.
- **SignedCertificateActionHandler**: Generates and attaches certificates.

## Extending the Engine

To add a new capability to the workflow engine:

1.  **Define the Model**: Create a new `Action` subclass in `validibot/actions/models.py`.
2.  **Implement Handler**: Create a class implementing `StepHandler` in `validibot/actions/handlers.py` (or a definition specific file).
3.  **Register**: Map the action type to your handler in `validibot/actions/registry.py`.

```python
# validibot/actions/registry.py
register_action_handler(MyActionType.CUSTOM, MyCustomHandler)
```
