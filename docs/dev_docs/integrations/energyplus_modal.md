# EnergyPlus Modal Integration

This note explains how the Django app talks to the `sv_modal` repository and what
to watch for when wiring workflow steps to the Modal-backed EnergyPlus runner.

## Repository layout

- `sv_modal/` lives at the project root (symlink or checkout). It exposes Modal
  callable modules under `sv_modal.projects.*`.
- The EnergyPlus runner lives in
  `sv_modal/projects/sv_energyplus/modal_app.py`.
  Modal registers two functions: `run_energyplus_simulation` and
  `cleanup_simulation_outputs`.
- Pydantic response models are defined in
  `sv_modal/projects/sv_energyplus/constants.py`. These are shared between the
  Modal function and any caller that wants typed access to the result payload.
- The Django validator expects each EnergyPlus ruleset to set
  `metadata["weather_file"]`. At runtime we fall back to the
  `ENERGYPLUS_DEFAULT_WEATHER` environment variable if metadata does not supply
  a value.

## Calling the runner

1. During Django startup (for example, in a Celery worker module) call
   `modal.Function.lookup("energyplus-runner", "run_energyplus_simulation")`.
   Cache the resulting proxy; lookups are relatively expensive.
2. Submit the payload with
   ```python
   result_dict = run_sim.call(
       epjson=epjson_payload,          # str | dict | bytes | Path
       weather_file="USA_CA_SF.epw",   # resolved relative to /inputs/weather_data
       simulation_id=stable_id,        # optional override
       return_logs=True,
   )
   ```
3. Deserialize with
   `SimulationResult.model_validate(result_dict)` if typed access is helpful.
4. Handle `"status" == "error"` by surfacing `errors`, `messages`, and
   `logs.err_tail` so operators can diagnose the run.
5. Delete `/outputs/<simulation_id>` via the `cleanup_simulation_outputs`
   function after downstream storage copies any required artifacts.

## Outstanding questions

- The Django app and Modal code both need the Pydantic models in
  `sv_modal/projects/sv_energyplus/constants.py`. Consider promoting them into a
  small shared package (for example, `sv_modal.shared.energyplus`) so imports
  remain stable if we ever split the repositories.
- Modal functions expect weather files bundled into the Docker image. If we need
  org-specific weather data we will have to extend the API to accept uploads or
  mount org volumes.
