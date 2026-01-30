# ADR: Validation Step Processor Refactor

**Status:** Implemented
**Date:** 2026-01-30
**Authors:** Daniel McQuillen, Claude

## Implementation Notes

This ADR was implemented on 2026-01-30. Key implementation details:

- All processor classes created in `validibot/validations/services/step_processor/`
- `ValidationRunService` updated to route validator steps through processors
- `ValidationCallbackService` updated to use `processor.complete_from_callback()`
- All 183 validation tests pass after implementation
- Action steps continue to use the existing handler flow (not refactored in this phase)

## Context

The validation step execution logic is currently spread across multiple locations:

1. **`ValidationRunService.execute_workflow_steps()`** - Orchestrates the workflow loop
2. **`ValidationRunService.execute_workflow_step()`** - Dispatches to handlers
3. **`ValidationRunService._record_step_result()`** - Persists findings and finalizes steps
4. **`ValidatorStepHandler.execute()`** - Bridges to validator engines
5. **`ValidationCallbackService._process_callback()`** - Handles async validator completion

This leads to significant code duplication, particularly between `_record_step_result()` and `_process_callback()`, which both:

- Persist `ValidationFinding` records
- Evaluate CEL assertions
- Extract signals for downstream steps
- Finalize step status with timing

Additionally, the current structure doesn't clearly separate:

- **Simple validators** (JSON Schema, XML Schema, Basic, AI) - run inline, single assertion stage
- **Advanced validators** (EnergyPlus, FMI) - run in containers, two assertion stages

## Decision

Introduce a `ValidationStepProcessor` abstraction with two concrete implementations:

- `SimpleValidationProcessor` - for inline validators
- `AdvancedValidationProcessor` - for container-based validators

**Critical design principle:** Processors handle lifecycle/orchestration only. All assertion evaluation happens in engines.

This follows the **Template Method pattern**, where the base class provides shared infrastructure and subclasses implement the specific validation flow.

## Terminology

| Term                   | Definition                                                                                          |
| ---------------------- | --------------------------------------------------------------------------------------------------- |
| **Simple validator**   | Validators that run inline in the Django process (Basic, JSON Schema, XML Schema, AI)               |
| **Advanced validator** | Validators packaged as Docker containers (EnergyPlus, FMI, user-added)                              |
| **Engine**             | The class that knows _how_ to validate AND evaluates assertions (e.g., `JsonSchemaValidatorEngine`) |
| **Processor**          | The class that knows the _lifecycle_ (call engine, persist results, handle errors, finalize)        |
| **Input stage**        | Assertion evaluation stage using input data (submission content) - all validators                   |
| **Output stage**       | Assertion evaluation stage using output data (container results) - advanced validators only         |

## Current State vs Target (Required Changes)

This section explicitly documents what differs between the current codebase and the target design. These are **required changes** as part of this refactor.

### 1. Input-stage assertions are NOT evaluated by all simple validators today

**Current:** Only `BasicValidatorEngine` and `AIValidatorEngine` evaluate CEL assertions in `validate()`. `JsonSchemaValidatorEngine` and `XmlSchemaValidatorEngine` do NOT evaluate assertions.

**Target:** All validators evaluate input-stage assertions in `validate()`.

**Decision:** Add CEL assertion evaluation to `JsonSchemaValidatorEngine.validate()` and `XmlSchemaValidatorEngine.validate()`. This ensures consistent behavior across all validator types - users can add CEL assertions to any workflow step regardless of the underlying validator engine. The implementation follows the same pattern already used by `BasicValidatorEngine`.

### 2. Output-stage assertions live in callback service, not engines

**Current:** `ValidationCallbackService._evaluate_output_stage_assertions()` handles output-stage CEL assertions using `engine.evaluate_cel_assertions()`. The logic lives in the callback service.

**Target:** Output-stage assertions are evaluated inside `engine.post_execute_validate()`.

**Required change:** Move assertion evaluation logic into advanced engines (EnergyPlus, FMI) and expose via `post_execute_validate()`.

### 3. Callback deletes existing findings (must change to append)

**Current:** `_process_callback()` deletes existing `ValidationFinding` records for the step before inserting envelope findings.

**Target:** Callbacks append output-stage findings to preserve input-stage assertion findings.

**Required change:** Modify `persist_findings()` to support append mode; update callback path to use it.

### 4. Assertion count keys are inconsistent

**Current:** Engines return `assertion_count` or `assertions_evaluated`. Summary builder looks for multiple keys via `_extract_assertion_total()`.

**Target:** Standardize on `assertion_total` everywhere.

**Required change:** Update all engines to use the typed `ValidationResult.assertion_stats` field. Remove `_extract_assertion_total()` helper entirely.

### 5. Engines don't return signals as first-class field

**Current:** `extract_output_signals()` is called separately by callback service after validation completes. Signals are not part of `ValidationResult`.

**Target:** `ValidationResult.signals` is a first-class field populated by `post_execute_validate()`.

**Required change:** Update advanced engine implementations to populate `ValidationResult.signals` directly.

## Responsibility Boundaries

### Engine Responsibilities

- **Validation logic**: Schema checking, container launching, AI prompting, etc.
- **Input-stage assertions**: Evaluated during `validate()` for all validator types
- **Output-stage assertions**: Evaluated during `post_execute_validate()` for advanced validators
- **Signal extraction**: Called internally by `post_execute_validate()` before assertion evaluation; signals returned in `stats["signals"]`
- Returns `ValidationResult` with issues, stats (including `signals`), and assertion outcomes

### Processor Responsibilities

- **Lifecycle orchestration**: Start step, call engine, handle completion, finalize step
- **Result persistence**: Save findings to database, update step status
- **Signal persistence**: Store `stats["signals"]` from engine result (no re-extraction)
- **Error handling**: Catch exceptions, record errors, set appropriate status
- **Sync/async coordination**: Handle both sync completion and async callback paths
- **Summary updates**: Store assertion counts in `step_run.output` for run summary

**Processors do NOT evaluate assertions or extract signals** - they only persist what the engine returns.

## Architecture

### Current Architecture

```
ValidationRunService.execute_workflow_steps()
    │
    ├── for each step:
    │       │
    │       ├── _start_step_run()
    │       │
    │       ├── execute_workflow_step()
    │       │       │
    │       │       └── ValidatorStepHandler.execute()
    │       │               │
    │       │               └── engine.validate()
    │       │
    │       └── _record_step_result()  ◄─── Duplicated logic
    │
    └── finalize run

ValidationCallbackService._process_callback()  ◄─── Duplicated logic
    │
    ├── download envelope
    ├── persist findings
    ├── evaluate assertions  ◄─── Currently only place output-stage runs!
    ├── extract signals
    ├── finalize step
    └── resume or finalize run
```

### Proposed Architecture

```
ValidationRunService.execute_workflow_steps()
    │
    ├── for each step:
    │       │
    │       ├── _start_step_run()
    │       │
    │       ├── processor = get_step_processor(step)
    │       │       │
    │       │       ├── SimpleValidationProcessor (JSON, XML, Basic, AI)
    │       │       └── AdvancedValidationProcessor (EnergyPlus, FMI)
    │       │
    │       └── result = processor.execute()
    │               │
    │               ├── calls engine.validate()  ◄─── Engine evaluates input-stage assertions
    │               ├── persists findings from engine result
    │               ├── [advanced only] calls engine.post_execute_validate()  ◄─── Engine evaluates output-stage assertions
    │               └── finalizes step
    │
    └── finalize run

ValidationCallbackService._process_callback()
    │
    ├── download envelope
    │
    └── processor = AdvancedValidationProcessor(...)
        └── processor.complete_from_callback(output_envelope)
                │
                ├── calls engine.post_execute_validate(envelope)  ◄─── Same code path!
                ├── persists findings (APPENDS, not replaces)
                └── finalizes step
```

## Engine Contract Changes

### New `post_execute_validate()` Method for Advanced Engines

Advanced validator engines must implement a `post_execute_validate()` method that:

1. Receives the output envelope from container execution
2. Extracts validation issues from envelope messages
3. Extracts output signals (metrics) from the envelope
4. Evaluates output-stage CEL assertions using those signals
5. Returns a `ValidationResult` with:
   - Combined issues (envelope messages + assertion findings)
   - Stats including `signals` dict for downstream steps (avoids double extraction)

```python
class BaseValidatorEngine(ABC):
    """Base class for all validator engines."""

    @abstractmethod
    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Perform validation and evaluate input-stage assertions.

        For simple validators: This is the only method called.
        For advanced validators: This launches the container and may return
        passed=None for async backends.

        Input-stage assertions are evaluated here for ALL validator types.
        """
        ...

    def post_execute_validate(
        self,
        output_envelope: Any,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Process container output and evaluate output-stage assertions.

        Only called for advanced validators after container completion.
        Called in two scenarios:
        1. Sync execution: Immediately after validate() returns with envelope
        2. Async execution: When callback arrives with envelope

        Implementation should:
        1. Extract issues from envelope.messages
        2. Extract signals via extract_output_signals()
        3. Evaluate output-stage CEL assertions using those signals
        4. Return ValidationResult with stats["signals"] containing the
           extracted signals (so processor doesn't need to extract again)

        Default implementation raises NotImplementedError. Advanced engines
        must override this.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support post_execute_validate(). "
            "This is required for advanced validators."
        )

    def extract_output_signals(self, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract signals from output for downstream CEL expressions.

        Called internally by post_execute_validate() for assertion evaluation.
        The extracted signals are returned in ValidationResult.stats["signals"]
        so the processor can persist them without calling this method again.

        Default returns None. Advanced engines override to extract metrics.
        """
        return None
```

### ExecutionResponse Must Carry Output Envelope

For sync backends (Docker Compose, local dev, test), the `ExecutionResponse` must include the `output_envelope` so that `post_execute_validate()` can be called:

```python
@dataclass
class ExecutionResponse:
    """Response from executing a validation container."""

    execution_id: str
    is_complete: bool
    output_envelope: Any | None = None  # REQUIRED for sync backends
    error_message: str | None = None
    input_uri: str | None = None
    output_uri: str | None = None
    execution_bundle_uri: str | None = None
    duration_seconds: float | None = None
```

**Migration note:** The `EnergyPlusValidationEngine._response_to_result()` method currently doesn't store the envelope. It must be updated to pass the envelope through.

**Current state:** `SelfHostedExecutionBackend` already populates `output_envelope` via `_read_output_envelope()`. `GCPExecutionBackend` does NOT (async - envelope delivered via callback).

### Output Envelope Type System

Container-based validators produce JSON output that is deserialized into domain-specific Pydantic models. This type system ensures engines receive strongly-typed data with validated fields, making engine code more readable and type-safe.

#### Type Hierarchy

```
ValidationOutputEnvelope (base)          # vb_shared.validations.envelopes
├── EnergyPlusOutputEnvelope             # vb_shared.energyplus.envelopes
│   └── outputs: EnergyPlusOutputs
│       └── metrics: EnergyPlusSimulationMetrics
│           ├── site_eui_kwh_m2: float | None
│           ├── site_electricity_kwh: float | None
│           ├── site_natural_gas_kwh: float | None
│           └── ... (domain-specific metrics)
│
└── FMIOutputEnvelope                    # vb_shared.fmi.envelopes
    └── outputs: FMIOutputs
        └── simulation_results: dict[str, Any]
            └── ... (FMU-specific outputs)
```

#### Base Envelope Structure

All output envelopes share a common structure:

```python
class ValidationOutputEnvelope(BaseModel):
    """Base output envelope from validator containers."""

    status: ValidationStatus           # SUCCESS, FAILED_VALIDATION, FAILED_RUNTIME, CANCELLED
    messages: list[ValidationMessage]  # Issues/warnings from validation
    validator: ValidatorInfo           # Metadata about the validator
    timing: TimingInfo                 # Start/end timestamps, duration
    outputs: BaseModel | None = None   # Domain-specific outputs (subclasses override)
```

#### Deserialization Flow

When a container completes, the backend deserializes raw JSON into the appropriate typed envelope:

```python
# In SelfHostedExecutionBackend._read_output_envelope()
def _read_output_envelope(self, output_path: str) -> ValidationOutputEnvelope | None:
    output_dict = json.loads(raw_json)

    # Route to domain-specific envelope based on validator type
    validator_type = output_dict.get("validator", {}).get("type", "").upper()

    if validator_type == "ENERGYPLUS":
        return EnergyPlusOutputEnvelope.model_validate(output_dict)
    if validator_type == "FMI":
        return FMIOutputEnvelope.model_validate(output_dict)

    # Fallback to generic envelope
    return ValidationOutputEnvelope.model_validate(output_dict)
```

#### Benefits for Engine Code

With typed envelopes, `post_execute_validate()` receives a specific type and can access fields directly:

```python
class EnergyPlusValidationEngine(BaseValidatorEngine):

    def post_execute_validate(
        self,
        output_envelope: EnergyPlusOutputEnvelope,  # Strongly typed!
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        # Type-safe access to domain-specific metrics
        metrics = output_envelope.outputs.metrics

        # IDE autocomplete works, type checker validates
        site_eui = metrics.site_eui_kwh_m2
        electricity = metrics.site_electricity_kwh

        # Extract signals for downstream assertions
        signals = self.extract_output_signals(output_envelope)

        # Evaluate output-stage CEL assertions using typed data
        assertion_findings = self.evaluate_cel_assertions(
            payload=signals,
            stage="output",
            run_context=run_context,
        )
        # ...
```

#### Signal Extraction Uses Typed Access

```python
class EnergyPlusValidationEngine(BaseValidatorEngine):

    def extract_output_signals(
        self,
        output_envelope: EnergyPlusOutputEnvelope,
    ) -> dict[str, Any] | None:
        outputs = output_envelope.outputs
        if not outputs or not outputs.metrics:
            return None

        # Pydantic model_dump gives us a clean dict
        # Filter None values (metrics not computed)
        metrics_dict = outputs.metrics.model_dump(mode="json")
        return {k: v for k, v in metrics_dict.items() if v is not None}
```

#### Type Safety Throughout the Stack

| Layer | Type | Source |
|-------|------|--------|
| Container output | Raw JSON | Docker stdout / GCS file |
| Backend response | `ValidationOutputEnvelope` subclass | `_read_output_envelope()` |
| Engine method | Domain-specific envelope | `post_execute_validate(envelope)` |
| Signals dict | `dict[str, Any]` | `extract_output_signals()` |
| CEL evaluation | Typed payload | `evaluate_cel_assertions(signals)` |

This type chain ensures:
1. **Validation at boundaries** - Pydantic validates JSON structure on deserialization
2. **IDE support** - Autocomplete and type hints throughout engine code
3. **Runtime safety** - Missing fields raise clear errors, not `KeyError`
4. **Documentation** - Types serve as documentation for expected data shapes

### Typed ValidationResult

The current `ValidationResult.stats` is a loose `dict[str, Any]`. This refactor replaces it with a structured result type to prevent drift and improve type safety:

```python
@dataclass
class AssertionStats:
    """Assertion evaluation statistics."""
    total: int = 0
    failures: int = 0


@dataclass
class ValidationResult:
    """
    Aggregated result of a single validation step.

    Attributes:
        passed: True/False for complete, None for pending (async).
        issues: List of validation issues discovered.
        assertion_stats: Structured assertion counts.
        signals: Extracted metrics for downstream steps (first-class field).
        output_envelope: For advanced validators, the typed container output.
        stats: Additional engine-specific metadata (execution_id, URIs, etc.).
    """
    passed: bool | None
    issues: list[ValidationIssue]
    assertion_stats: AssertionStats = field(default_factory=AssertionStats)
    signals: dict[str, Any] | None = None  # First-class, not buried in stats
    output_envelope: Any | None = None      # Typed envelope for advanced validators
    stats: dict[str, Any] | None = None     # Engine-specific metadata only
```

**Benefits:**
- IDE autocomplete and type checking for assertion counts
- `signals` as a first-class field avoids subtle key-lookup bugs
- Clear separation: structured fields vs. loose metadata
- Easier to reason about and test

**Key invariants:**
- `assertion_stats` is always present (defaults to `AssertionStats(0, 0)`)
- `signals` is populated by `post_execute_validate()` for advanced validators (may be empty dict)
- `output_envelope` is populated for sync advanced validators
- `stats` contains only engine-specific metadata (execution_id, URIs, timing, etc.)

**Implementation:** Update `ValidationResult` in `engines/base.py` as part of Phase 0. All engines must be updated to use the new fields.

### Assertion Evaluation Consistency

All validators that support CEL assertions must follow the same pattern to ensure consistent behavior:

| Validator | Input-stage assertions | Output-stage assertions | Payload shape |
|-----------|------------------------|-------------------------|---------------|
| Basic | ✓ `validate()` | N/A | Parsed JSON/XML |
| AI | ✓ `validate()` | N/A | AI response dict |
| JSON Schema | ✓ `validate()` (to add) | N/A | Parsed JSON |
| XML Schema | ✓ `validate()` (to add) | N/A | Parsed XML dict |
| EnergyPlus | ✓ `validate()` | ✓ `post_execute_validate()` | Submission / Metrics |
| FMI | ✓ `validate()` | ✓ `post_execute_validate()` | Submission / Output values |

**Consistency requirements for JSON/XML:**
1. Call `evaluate_cel_assertions(..., target_stage="input")` in `validate()`
2. Use parsed submission content as the payload (same as Basic validator)
3. Return `assertion_total` and `assertion_failures` in `stats`
4. Include assertion issues in the returned `ValidationResult.issues`

**Reference implementation:** See `BasicValidatorEngine.validate()` for the canonical pattern.

## Detailed Design

### Base Class: `ValidationStepProcessor`

```python
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from typing import Any

from validibot.validations.constants import StepStatus
from validibot.validations.engines.base import AssertionStats, ValidationResult
from validibot.validations.models import (
    ValidationFinding,
    ValidationRun,
    ValidationStepRun,
)


@dataclass
class StepProcessingResult:
    """Result of processing a validation step."""

    passed: bool | None  # None = async, waiting for callback
    step_run: ValidationStepRun
    severity_counts: Counter
    total_findings: int
    assertion_failures: int
    assertion_total: int


class ValidationStepProcessor(ABC):
    """
    Base class for processing a single validation step.

    Processors handle LIFECYCLE ONLY:
    - Call engine methods at the right time
    - Persist findings from engine results
    - Handle errors and set appropriate status
    - Finalize step with timing

    Processors do NOT evaluate assertions - that's the engine's job.
    """

    def __init__(
        self,
        validation_run: ValidationRun,
        step_run: ValidationStepRun,
    ):
        self.validation_run = validation_run
        self.step_run = step_run
        self.workflow_step = step_run.workflow_step
        self.validator = self.workflow_step.validator
        self.ruleset = self.workflow_step.ruleset

    @abstractmethod
    def execute(self) -> StepProcessingResult:
        """Execute the validation step. Subclasses implement this."""
        ...

    # ──────────────────────────────────────────────────────────────
    # Shared methods used by both subclasses
    # ──────────────────────────────────────────────────────────────

    def _get_engine(self):
        """Get the validator engine from the registry."""
        from validibot.validations.engines.registry import get as get_engine
        return get_engine(self.validator.validation_type)

    def _build_run_context(self):
        """Build RunContext for engine calls."""
        from validibot.actions.protocols import RunContext
        return RunContext(
            validation_run=self.validation_run,
            step=self.workflow_step,
            downstream_signals=self._get_downstream_signals(),
        )

    def _get_downstream_signals(self) -> dict[str, Any]:
        """Extract signals from prior steps for cross-step assertions."""
        summary = self.validation_run.summary or {}
        return summary.get("steps", {})

    def persist_findings(
        self,
        issues: list,
        *,
        append: bool = False,
    ) -> tuple[Counter, int]:
        """
        Persist ValidationFinding records from issues.

        Args:
            issues: List of ValidationIssue objects from engine
            append: If True, add to existing findings. If False, replace.
                    Default False for simple validators, True for async callbacks.

        Returns:
            Tuple of (severity_counts, assertion_failures)
        """
        # Implementation extracted from ValidationRunService._persist_findings()
        ...

    def store_assertion_counts(
        self,
        assertion_failures: int,
        assertion_total: int,
    ) -> None:
        """
        Store assertion counts in step_run.output for run summary.

        These fields are used by _build_run_summary_record() to calculate
        overall assertion pass/fail counts.
        """
        output = self.step_run.output or {}
        output["assertion_failures"] = assertion_failures
        output["assertion_total"] = assertion_total
        self.step_run.output = output
        self.step_run.save(update_fields=["output"])

    def store_signals(self, signals: dict) -> None:
        """
        Store signals in run.summary for downstream steps.

        Signals are already extracted by the engine (during assertion evaluation)
        and passed here via ValidationResult.stats["signals"]. The processor
        just persists them.

        Signals are stored at: run.summary["steps"][step_run_id]["signals"]
        """
        # Implementation extracted from validation_callback.py lines 677-684
        ...

    def finalize_step(
        self,
        status: StepStatus,
        stats: dict,
        error: str | None = None,
    ) -> None:
        """
        Mark step complete with timing and output.

        Sets ended_at, duration_ms, status, output, and error fields.
        """
        # Implementation extracted from ValidationRunService._finalize_step_run()
        ...
```

### Simple Validator Processor

```python
class SimpleValidationProcessor(ValidationStepProcessor):
    """
    Processor for simple validators (JSON Schema, XML Schema, Basic, AI).

    These validators:
    - Run inline in the Django process
    - Complete synchronously
    - Have a single assertion stage (input)

    The engine handles ALL validation logic including input-stage assertions.
    The processor just calls engine.validate() and persists results.
    """

    def execute(self) -> StepProcessingResult:
        engine = self._get_engine()
        run_context = self._build_run_context()

        try:
            # Call engine.validate() - this does EVERYTHING:
            # - Validation logic (schema checking, AI prompting, etc.)
            # - Input-stage CEL assertion evaluation
            # - Returns combined issues with assertion outcomes
            result = engine.validate(
                validator=self.validator,
                submission=self.validation_run.submission,
                ruleset=self.ruleset,
                run_context=run_context,
            )
        except Exception as e:
            return self._handle_error(e)

        # Persist findings from engine result
        severity_counts, assertion_failures = self.persist_findings(result.issues)

        # Store assertion counts for run summary (using typed fields)
        self.store_assertion_counts(
            result.assertion_stats.failures,
            result.assertion_stats.total,
        )

        # Finalize the step
        status = StepStatus.PASSED if result.passed else StepStatus.FAILED
        self.finalize_step(status, result.stats or {})

        return StepProcessingResult(
            passed=result.passed,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=sum(severity_counts.values()),
            assertion_failures=result.assertion_stats.failures,
            assertion_total=result.assertion_stats.total,
        )

    def _handle_error(self, error: Exception) -> StepProcessingResult:
        """Handle validation errors gracefully."""
        from validibot.validations.engines.base import ValidationIssue
        from validibot.validations.constants import Severity

        issues = [
            ValidationIssue(
                path="",
                message=str(error),
                severity=Severity.ERROR,
            )
        ]
        severity_counts, _ = self.persist_findings(issues)
        self.finalize_step(StepStatus.FAILED, {}, error=str(error))

        return StepProcessingResult(
            passed=False,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=1,
            assertion_failures=0,
            assertion_total=0,
        )
```

### Advanced Validator Processor

```python
class AdvancedValidationProcessor(ValidationStepProcessor):
    """
    Processor for advanced validators (EnergyPlus, FMI).

    These validators:
    - Run in Docker containers
    - May complete synchronously (Docker Compose) or asynchronously (GCP)
    - Have two assertion stages (input and output)

    ## Execution Modes

    **Synchronous (Docker Compose, local dev, test):**
    - Container runs and blocks until complete
    - `execute()` calls `engine.post_execute_validate()` directly
    - Returns complete result immediately

    **Asynchronous (GCP Cloud Run, future AWS):**
    - Container job is launched and returns immediately
    - `execute()` returns `passed=None` (pending)
    - Later, callback arrives and `complete_from_callback()` is called

    ## Assertion Stages

    **Input stage:** Evaluated in `engine.validate()` BEFORE container launch.
    **Output stage:** Evaluated in `engine.post_execute_validate()` AFTER container completes.
    """

    def execute(self) -> StepProcessingResult:
        engine = self._get_engine()
        run_context = self._build_run_context()

        try:
            # Call engine.validate() - this:
            # - Evaluates input-stage CEL assertions
            # - Launches the container
            # - For sync backends: blocks and returns with output_envelope
            # - For async backends: returns immediately with passed=None
            result = engine.validate(
                validator=self.validator,
                submission=self.validation_run.submission,
                ruleset=self.ruleset,
                run_context=run_context,
            )
        except Exception as e:
            return self._handle_error(e)

        # Persist input-stage findings (from assertions evaluated in validate())
        severity_counts, assertion_failures = self.persist_findings(result.issues)

        # Handle sync vs async completion
        if result.passed is None:
            # Async execution - container launched, waiting for callback
            self._record_pending_state(result)
            return StepProcessingResult(
                passed=None,
                step_run=self.step_run,
                severity_counts=severity_counts,
                total_findings=sum(severity_counts.values()),
                assertion_failures=result.assertion_stats.failures,
                assertion_total=result.assertion_stats.total,
            )
        else:
            # Sync execution - container completed, call post_execute_validate()
            if result.output_envelope is None:
                return self._handle_missing_envelope()
            return self._complete_with_envelope(engine, run_context, result.output_envelope, severity_counts)

    def complete_from_callback(self, output_envelope: Any) -> StepProcessingResult:
        """
        Complete the step after receiving async callback.

        Called by ValidationCallbackService after downloading the output
        envelope from cloud storage.

        IMPORTANT: This APPENDS findings to existing ones (from input-stage
        assertions). It does NOT delete pre-existing findings.
        """
        engine = self._get_engine()
        run_context = self._build_run_context()

        # Get existing severity counts from input-stage findings
        existing_counts = self._get_existing_finding_counts()

        return self._complete_with_envelope(
            engine,
            run_context,
            output_envelope,
            existing_counts,
            append_findings=True,
        )

    def _complete_with_envelope(
        self,
        engine,
        run_context,
        output_envelope: Any,
        existing_severity_counts: Counter,
        *,
        append_findings: bool = False,
    ) -> StepProcessingResult:
        """
        Complete the step using the output envelope.

        Called by both sync execution and async callback paths.
        """
        # Call engine.post_execute_validate() - this:
        # 1. Extracts signals from envelope (for assertion evaluation)
        # 2. Evaluates output-stage CEL assertions using those signals
        # 3. Extracts issues from envelope messages
        # 4. Returns ValidationResult with signals field populated
        post_result = engine.post_execute_validate(output_envelope, run_context)

        # Persist output-stage findings (APPEND for callbacks)
        output_counts, output_assertion_failures = self.persist_findings(
            post_result.issues,
            append=append_findings,
        )

        # Merge severity counts
        severity_counts = existing_severity_counts + output_counts

        # Store signals for downstream steps (using typed field)
        self.store_signals(post_result.signals or {})

        # Calculate total assertion counts (input + output stages)
        input_stats = self._get_stored_assertion_stats()
        assertion_total = input_stats.total + post_result.assertion_stats.total
        assertion_failures = input_stats.failures + post_result.assertion_stats.failures

        # Store final assertion counts
        self.store_assertion_counts(assertion_failures, assertion_total)

        # Determine final status
        status = self._map_envelope_status(output_envelope.status)
        error = self._extract_error(output_envelope)

        # Include full envelope in step output (JSON-safe serialization)
        stats = self._serialize_envelope(output_envelope)
        self.finalize_step(status, stats, error)

        return StepProcessingResult(
            passed=(status == StepStatus.PASSED),
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=sum(severity_counts.values()),
            assertion_failures=assertion_failures,
            assertion_total=assertion_total,
        )

    def _record_pending_state(self, result: ValidationResult) -> None:
        """Record execution metadata and input-stage assertion stats for async steps."""
        self.step_run.output = {
            "assertion_total": result.assertion_stats.total,
            "assertion_failures": result.assertion_stats.failures,
            **(result.stats or {}),
        }
        self.step_run.status = StepStatus.RUNNING
        self.step_run.save(update_fields=["output", "status"])

    def _get_stored_assertion_stats(self) -> AssertionStats:
        """Get assertion stats stored from input-stage (for callback path)."""
        output = self.step_run.output or {}
        return AssertionStats(
            total=output.get("assertion_total", 0),
            failures=output.get("assertion_failures", 0),
        )

    def _get_existing_finding_counts(self) -> Counter:
        """Get severity counts from existing findings (for callback path)."""
        from validibot.validations.models import ValidationFinding

        counts = Counter()
        findings = ValidationFinding.objects.filter(step_run=self.step_run)
        for finding in findings:
            counts[finding.severity] += 1
        return counts

    def _map_envelope_status(self, envelope_status) -> StepStatus:
        """Map ValidationStatus from envelope to StepStatus."""
        from vb_shared.validations.envelopes import ValidationStatus

        mapping = {
            ValidationStatus.SUCCESS: StepStatus.PASSED,
            ValidationStatus.FAILED_VALIDATION: StepStatus.FAILED,
            ValidationStatus.FAILED_RUNTIME: StepStatus.FAILED,
            ValidationStatus.CANCELLED: StepStatus.SKIPPED,  # Aligned: both sync and async
        }
        return mapping.get(envelope_status, StepStatus.FAILED)

    def _extract_error(self, output_envelope) -> str:
        """Extract error message from envelope."""
        from vb_shared.validations.envelopes import ValidationStatus

        if output_envelope.status == ValidationStatus.SUCCESS:
            return ""

        error_messages = [
            msg.text for msg in output_envelope.messages
            if str(msg.severity) == "ERROR"
        ]
        return "\n".join(error_messages)

    def _serialize_envelope(self, output_envelope) -> dict:
        """Serialize envelope to JSON-safe dict for step_run.output."""
        if hasattr(output_envelope, "model_dump"):
            return output_envelope.model_dump(mode="json")
        if isinstance(output_envelope, dict):
            return output_envelope
        return {}

    def _handle_error(self, error: Exception) -> StepProcessingResult:
        """Handle validation errors gracefully."""
        from validibot.validations.engines.base import ValidationIssue
        from validibot.validations.constants import Severity

        issues = [
            ValidationIssue(
                path="",
                message=str(error),
                severity=Severity.ERROR,
            )
        ]
        severity_counts, _ = self.persist_findings(issues)
        self.finalize_step(StepStatus.FAILED, {}, error=str(error))

        return StepProcessingResult(
            passed=False,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=1,
            assertion_failures=0,
            assertion_total=0,
        )

    def _handle_missing_envelope(self) -> StepProcessingResult:
        """Handle case where sync backend didn't return envelope."""
        from validibot.validations.engines.base import ValidationIssue
        from validibot.validations.constants import Severity

        issues = [
            ValidationIssue(
                path="",
                message="Sync execution completed but no output envelope received. "
                        "This indicates a backend configuration issue.",
                severity=Severity.ERROR,
            )
        ]
        severity_counts, _ = self.persist_findings(issues)
        self.finalize_step(StepStatus.FAILED, {}, error="Missing output envelope")

        return StepProcessingResult(
            passed=False,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=1,
            assertion_failures=0,
            assertion_total=0,
        )
```

### Factory Function

```python
def get_step_processor(
    validation_run: ValidationRun,
    step_run: ValidationStepRun,
) -> ValidationStepProcessor:
    """
    Get the appropriate processor for a validation step.

    Routes to SimpleValidationProcessor or AdvancedValidationProcessor
    based on the validator type.
    """
    from validibot.validations.constants import ValidationType

    validator = step_run.workflow_step.validator

    # Advanced validators run in containers
    advanced_types = {
        ValidationType.ENERGYPLUS,
        ValidationType.FMI,
    }

    if validator.validation_type in advanced_types:
        return AdvancedValidationProcessor(validation_run, step_run)
    else:
        return SimpleValidationProcessor(validation_run, step_run)
```

## ActionProcessor (Future)

The same pattern will be applied to action steps:

```python
class ActionStepProcessor(ABC):
    """Base class for processing action steps."""

    @abstractmethod
    def execute(self) -> StepProcessingResult:
        """Execute the action step."""
        ...


def get_action_processor(
    validation_run: ValidationRun,
    step_run: ValidationStepRun,
) -> ActionStepProcessor:
    """Get the appropriate processor for an action step."""
    ...
```

This is out of scope for this ADR but noted for completeness.

## Explicit Behavior Changes

This refactor introduces the following behavior changes that must be acknowledged:

### 1. Output-stage assertions now run for sync advanced validators

**Current:** Output-stage CEL assertions only run in the callback path (async).
**New:** Output-stage assertions run for both sync AND async advanced validators.

**Impact:** Sync Docker Compose deployments will now evaluate output-stage assertions. This is the intended behavior but is a change from current behavior.

### 2. CANCELLED status mapping aligned

**Current:** Sync advanced validators map CANCELLED → FAILED. Async callbacks map CANCELLED → SKIPPED.
**New:** Both paths map CANCELLED → SKIPPED consistently.

**Impact:** ValidationRun summaries may show different status distributions. This is a correction.

### 3. Callback no longer deletes existing findings

**Current:** `_process_callback()` may delete/replace findings for the step.
**New:** `complete_from_callback()` appends output-stage findings to existing input-stage findings.

**Impact:** Steps will retain input-stage assertion findings when callback arrives. This prevents losing pre-execution assertion results.

### 4. Assertion counts stored consistently

**Current:** `step_run.output` may or may not have `assertion_failures` and `assertion_total` fields depending on code path.
**New:** Both fields always set via `store_assertion_counts()` for both simple and advanced validators.

**Impact:** Run summary assertion counts will be consistent across all validator types and execution modes.

## Callback Persistence Rules

> **⚠️ KEY BEHAVIOR CHANGE:** The current callback implementation DELETES existing findings before inserting new ones. This refactor changes to APPEND mode, preserving input-stage assertion findings when the callback arrives.

When processing callbacks for async advanced validators:

1. **DO NOT** delete existing `ValidationFinding` records for the step
2. **APPEND** output-stage findings to existing input-stage findings
3. **READ** existing assertion counts from `step_run.output` before adding output-stage counts
4. **UPDATE** `step_run.output` with combined assertion counts

This ensures input-stage assertions (evaluated before container launch) are preserved when the callback arrives with output-stage results.

**Why this matters:** Without this change, any CEL assertions that ran before container launch (input-stage) would be lost when the callback arrives with output-stage results. Users would see incomplete assertion results.

## Workflow Examples

### Example 1: Simple Validator (JSON Schema)

**Scenario:** User submits a JSON file to be validated against a JSON Schema.

```
1. ValidationRunService.execute_workflow_steps() starts

2. For the JSON Schema step:

   a. _start_step_run() creates ValidationStepRun with status=RUNNING

   b. processor = get_step_processor(run, step_run)
      → Returns SimpleValidationProcessor

   c. processor.execute():

      i.   engine = JsonSchemaValidatorEngine()

      ii.  result = engine.validate(...)
           - Loads JSON Schema from ruleset
           - Parses submission JSON
           - Runs jsonschema validation
           - Evaluates input-stage CEL assertions
           - Returns ValidationResult(passed=True/False, issues=[...], stats={assertion_total: N})

      iii. processor.persist_findings(result.issues)
           - Creates ValidationFinding records for each issue
           - Returns (severity_counts, assertion_failures)

      iv.  processor.store_assertion_counts(failures, total)
           - Saves to step_run.output for run summary

      v.   processor.finalize_step(StepStatus.PASSED, stats)
           - Sets step_run.ended_at, duration_ms, status, output

      vi.  Returns StepProcessingResult(passed=True, ...)

3. Run completes with status=SUCCEEDED
```

### Example 2: Advanced Validator - Sync Execution (Docker Compose)

**Scenario:** User runs EnergyPlus validation on a self-hosted deployment using Docker.

```
1. ValidationRunService.execute_workflow_steps() starts

2. For the EnergyPlus step:

   a. _start_step_run() creates ValidationStepRun with status=RUNNING

   b. processor = get_step_processor(run, step_run)
      → Returns AdvancedValidationProcessor

   c. processor.execute():

      i.   engine = EnergyPlusValidationEngine()

      ii.  result = engine.validate(...)
           Inside engine.validate():
           - Evaluates input-stage CEL assertions
           - backend = get_execution_backend()
             → Returns SelfHostedExecutionBackend (is_async=False)
           - response = backend.execute(request)
             → Runs Docker container, BLOCKS until complete
             → Returns ExecutionResponse with output_envelope  ◄─── REQUIRED
           - Returns ValidationResult(passed=True, stats={output_envelope: ...})

      iii. processor.persist_findings(result.issues)
           - Persists input-stage assertion findings

      iv.  result.passed is NOT None (sync backend)
           output_envelope = stats["output_envelope"]

      v.   processor._complete_with_envelope(engine, run_context, envelope, counts):

           a. post_result = engine.post_execute_validate(envelope, run_context)
              Inside engine.post_execute_validate():
              1. Extracts signals via extract_output_signals()
              2. Evaluates output-stage CEL assertions using those signals
              3. Extracts issues from envelope.messages
              4. Returns ValidationResult with:
                 - issues: combined envelope messages + assertion findings
                 - stats["signals"]: extracted metrics for downstream steps

           b. processor.persist_findings(post_result.issues)
              → Creates ValidationFinding records

           c. processor.store_signals(post_result.stats["signals"])
              → Stores metrics in run.summary["steps"][id]["signals"]
              → No double extraction - uses signals already computed by engine

           d. processor.store_assertion_counts(total_failures, total_total)

           e. processor.finalize_step(StepStatus.PASSED, stats)

      vi.  Returns StepProcessingResult(passed=True, ...)

3. Run completes with status=SUCCEEDED
```

### Example 3: Advanced Validator - Async Execution (GCP Cloud Run)

**Scenario:** User runs EnergyPlus validation on GCP using Cloud Run Jobs.

```
PHASE 1: Launch Container
──────────────────────────

1. ValidationRunService.execute_workflow_steps() starts

2. For the EnergyPlus step:

   a. _start_step_run() creates ValidationStepRun with status=RUNNING

   b. processor = get_step_processor(run, step_run)
      → Returns AdvancedValidationProcessor

   c. processor.execute():

      i.   engine = EnergyPlusValidationEngine()

      ii.  result = engine.validate(...)
           Inside engine.validate():
           - Evaluates input-stage CEL assertions
           - backend = get_execution_backend()
             → Returns GCPExecutionBackend (is_async=True)
           - response = backend.execute(request)
             → Uploads input envelope to GCS
             → Triggers Cloud Run Job
             → Returns IMMEDIATELY
           - Returns ValidationResult(passed=None, stats={execution_id: "job-123"})

      iii. processor.persist_findings(result.issues)
           - Persists input-stage assertion findings

      iv.  result.passed IS None (async backend)
           processor._record_pending_state(stats)
           - step_run.output = {execution_id: ..., assertion_failures: N, assertion_total: M}
           - step_run.status = RUNNING

      v.   Returns StepProcessingResult(passed=None, ...)

   d. execute_workflow_steps() sees passed=None
      → Sets pending_async = True
      → Returns WITHOUT finalizing run (status stays RUNNING)

3. Run stays in RUNNING state, waiting for callback


PHASE 2: Callback Processing (minutes later)
────────────────────────────────────────────

4. Cloud Run Job completes:
   - Writes EnergyPlusOutputEnvelope to GCS
   - POSTs callback to /api/internal/callbacks/validation/

5. ValidationCallbackService.process(payload):

   a. Validates callback payload (Pydantic model)
   b. Idempotency check via CallbackReceipt
   c. Downloads output envelope from GCS

   d. processor = AdvancedValidationProcessor(run, step_run)
      result = processor.complete_from_callback(output_envelope)

      Inside complete_from_callback():

      i.   existing_counts = _get_existing_finding_counts()
           → Reads input-stage finding counts (NOT deleted!)

      ii.  _complete_with_envelope(engine, run_context, envelope, existing_counts, append=True):

           a. post_result = engine.post_execute_validate(envelope, run_context)
              → Engine extracts signals, evaluates output-stage assertions
              → Returns result with `signals` field populated

           b. processor.persist_findings(post_result.issues, append=True)
              → APPENDS findings, preserves input-stage findings

           c. processor.store_signals(post_result.signals)
              → Uses signals already extracted by engine (no double extraction)
           d. processor.store_assertion_counts(combined_failures, combined_total)
           e. processor.finalize_step(StepStatus.PASSED, stats)

      iii. Returns StepProcessingResult(passed=True, ...)

   e. Check for remaining steps:
      IF more steps exist AND step passed:
          → enqueue_validation_run(resume_from_step=next_step)
      ELSE:
          → Finalize run (status=SUCCEEDED or FAILED)

6. Run completes with status=SUCCEEDED
```

## File Changes

### New Files

| File                                                        | Purpose                              |
| ----------------------------------------------------------- | ------------------------------------ |
| `validibot/validations/services/step_processor/__init__.py` | Package exports                      |
| `validibot/validations/services/step_processor/base.py`     | `ValidationStepProcessor` base class |
| `validibot/validations/services/step_processor/simple.py`   | `SimpleValidationProcessor`          |
| `validibot/validations/services/step_processor/advanced.py` | `AdvancedValidationProcessor`        |
| `validibot/validations/services/step_processor/factory.py`  | `get_step_processor()` factory       |
| `validibot/validations/services/step_processor/result.py`   | `StepProcessingResult` dataclass     |

### Modified Files

| File                                                      | Changes                                                            |
| --------------------------------------------------------- | ------------------------------------------------------------------ |
| `validibot/validations/services/validation_run.py`        | Simplify to use processor; remove duplicated logic                 |
| `validibot/validations/services/validation_callback.py`   | Use `AdvancedValidationProcessor.complete_from_callback()`         |
| `validibot/validations/engines/base.py`                   | Add typed `ValidationResult` fields, `AssertionStats`, `post_execute_validate()` |
| `validibot/validations/engines/energyplus.py`             | Implement `post_execute_validate()`, return signals in stats       |
| `validibot/validations/engines/fmi.py`                    | Implement `post_execute_validate()`, return signals in stats       |
| `validibot/validations/engines/json.py`                   | Add CEL assertion evaluation to `validate()`                       |
| `validibot/validations/engines/xml.py`                    | Add CEL assertion evaluation to `validate()`                       |
| `validibot/validations/engines/basic.py`                  | Use typed `assertion_stats` and `signals` fields                   |
| `validibot/validations/engines/ai.py`                     | Use typed `assertion_stats` and `signals` fields                   |
| `validibot/validations/services/execution/self_hosted.py` | Ensure `ExecutionResponse.output_envelope` is populated (already ✓)|
| `validibot/actions/handlers.py`                           | May simplify `ValidatorStepHandler`                                |

### Removed/Deprecated

The following methods in `ValidationRunService` will be simplified or removed:

- `_record_step_result()` - Logic moves to processors
- `_persist_findings()` - Moves to `ValidationStepProcessor.persist_findings()`
- `_finalize_step_run()` - Moves to `ValidationStepProcessor.finalize_step()`
- `_normalize_issue()` - Moves to processor base class
- `_coerce_severity()` - Moves to processor base class

## Migration Strategy

### Phase 0: Prerequisite Engine Changes (no processor changes yet)

These changes prepare engines to support the new contract:

1. **Update `ValidationResult` to typed structure**
   - Add `AssertionStats` dataclass to `engines/base.py`
   - Add `assertion_stats`, `signals`, `output_envelope` fields to `ValidationResult`
   - Remove `_extract_assertion_total()` helper (no longer needed)

2. **Update all engines to use typed fields**
   - Replace `stats["assertion_total"]` with `assertion_stats.total`
   - Replace `stats["assertion_failures"]` with `assertion_stats.failures`
   - Use `signals` field instead of `stats["signals"]`
   - Use `output_envelope` field instead of `stats["output_envelope"]`

3. **Add CEL assertions to JSON/XML validators**
   - Add `evaluate_cel_assertions()` call to `JsonSchemaValidatorEngine.validate()`
   - Add `evaluate_cel_assertions()` call to `XmlSchemaValidatorEngine.validate()`
   - Follow the same pattern used by `BasicValidatorEngine`

4. **Implement `post_execute_validate()` in advanced engines**
   - Add `post_execute_validate()` to `BaseValidatorEngine` with `NotImplementedError`
   - Implement in `EnergyPlusValidationEngine`:
     - Move output-stage assertion logic from `ValidationCallbackService._evaluate_output_stage_assertions()`
     - Populate `signals` field directly
   - Implement in `FMUValidationEngine` (same pattern)

5. **Ensure envelope flows through sync backends**
   - `SelfHostedExecutionBackend` already populates `output_envelope` ✓
   - Update `EnergyPlusValidationEngine._response_to_result()` to include envelope in stats

### Phase 1: Create Processor Classes (no behavior change)

- Implement `ValidationStepProcessor`, `SimpleValidationProcessor`, `AdvancedValidationProcessor`
- Processors initially delegate to existing service methods
- Add comprehensive tests for processor classes

### Phase 2: Migrate Logic to Processors

- Move `_persist_findings()` logic into processor base class
- Move `_finalize_step_run()` logic into processor base class
- Add `append` mode to `persist_findings()` for callback path
- Update processors to use internal methods

### Phase 3: Update ValidationRunService

- Replace inline execution with processor calls
- Remove duplicated methods
- Simplify `_record_step_result()` to just metrics extraction

### Phase 4: Update ValidationCallbackService

- Replace `_process_callback()` logic with `complete_from_callback()` call
- Remove duplicated finding/signal/finalize code
- **Critical:** Change from delete+insert to append for findings

### Phase 5: Cleanup

- Remove deprecated methods from `ValidationRunService`
- Remove `_evaluate_output_stage_assertions()` from `ValidationCallbackService`
- Update tests
- Update documentation

### Phase 6: Code Quality Improvements

As part of this refactor, address the following code quality issues in `ValidationRunService`:

#### 1. Add detailed inline comments to `execute_workflow_steps()`

The relationship between these methods is not immediately clear:
- `execute_workflow_steps()` - The main entry point that loops through steps
- `_start_step_run()` - Creates/retrieves the `ValidationStepRun` record
- `execute_workflow_step()` - Dispatches a step to its handler
- `_record_step_result()` - Persists findings and finalizes the step

Add comments explaining:
- Why `_start_step_run()` is separate (idempotency via `get_or_create`)
- The difference between step creation and step execution
- How the processor replaces the middle two methods

#### 2. Consistent underscore prefix convention

**Current inconsistency:**
- `_start_step_run()` - has prefix (suggests "private")
- `_finalize_step_run()` - has prefix
- `execute_workflow_step()` - NO prefix (but also never called externally)

**Convention to adopt:**
- `_` prefix = internal implementation detail, not part of public API
- No prefix = public API that external code may call

**Recommendation:** Since `execute_workflow_step()` is only called from within `execute_workflow_steps()`, it should be renamed to `_execute_workflow_step()` for consistency. However, with the processor refactor, this method may be removed entirely as the processor takes over dispatching to engines.

**Decision:** Document current naming, and when introducing processors, use consistent naming:
- Public: `execute_workflow_steps()` (entry point, called by task dispatcher)
- Private: All helper methods get `_` prefix

This will be naturally addressed as we move logic into processor classes, which have their own clear public interface (`execute()`, `complete_from_callback()`).

## Testing Strategy

### Unit Tests

- `test_simple_processor.py` - Test SimpleValidationProcessor with mocked engine
- `test_advanced_processor.py` - Test AdvancedValidationProcessor with mocked backend
- `test_processor_factory.py` - Test factory routing logic
- `test_engine_post_execute_validate.py` - Test engine.post_execute_validate() implementations

### Integration Tests

- Test full workflow with JSON Schema validator
- Test full workflow with EnergyPlus (sync backend)
- Test callback flow with mocked async backend
- Test input/output-stage assertions for advanced validators
- Test that callback preserves input-stage findings

### Regression Tests

- Ensure existing behavior unchanged (except documented changes)
- Run full test suite after each phase
- Compare finding counts, assertion results, step timing
- Verify assertion counts in run summaries

## Alternatives Considered

### 1. Keep Current Structure, Just Extract Shared Methods

**Rejected because:** Still requires coordinating between `ValidationRunService` and `ValidationCallbackService`. The callback service would need to call back into the run service, creating awkward dependencies.

### 2. Single Processor Class with Mode Flag

```python
class ValidationStepProcessor:
    def __init__(self, ..., is_advanced: bool):
        self.is_advanced = is_advanced
```

**Rejected because:** Leads to conditional logic throughout the class. The Template Method pattern with subclasses is cleaner and more maintainable.

### 3. Move All Logic to Engines (Original ADR)

Have engines handle finding persistence, assertion evaluation, etc.

**Rejected because:** Engines should focus on _how_ to validate. They shouldn't know about Django models, step lifecycle, or run summaries. The revised design keeps assertion evaluation in engines (where it logically belongs) while keeping persistence/lifecycle in processors.

### 4. Processor Evaluates Assertions

Original ADR had processors calling `engine.evaluate_cel_assertions()`.

**Rejected because:**

- Some engines (Basic, AI) already evaluate assertions in `validate()` - would double-count
- Separating "validation logic" from "assertion evaluation" is artificial
- Engines know best how to extract assertion payloads from their specific data structures

## Consequences

### Benefits

1. **DRY:** Single code path for finding persistence, step finalization
2. **Testability:** Processors are easy to unit test with mocked engines
3. **Clarity:** Clear separation - engine (validation + assertions) vs processor (lifecycle)
4. **Consistency:** Same behavior for sync and async advanced validators
5. **Maintainability:** Changes to step lifecycle affect one place

### Drawbacks

1. **Migration effort:** Requires careful phased migration
2. **New abstraction:** Another layer for developers to understand
3. **Engine contract change:** Advanced engines must implement `post_execute_validate()`

### Risks

1. **Regression risk:** Must ensure all edge cases preserved during migration
2. **Callback timing:** Must handle race conditions between callback and processor
3. **Transaction boundaries:** Must maintain correct DB transaction scopes
4. **Double-evaluation:** Must ensure engines that already evaluate assertions aren't called twice

## References

- [Template Method Pattern](https://refactoring.guru/design-patterns/template-method)
- [validation_run.py](validibot/validations/services/validation_run.py) - Current implementation
- [validation_callback.py](validibot/validations/services/validation_callback.py) - Callback service
- [engines/base.py](validibot/validations/engines/base.py) - Engine base class
- [engines/energyplus.py](validibot/validations/engines/energyplus.py) - Advanced engine example
