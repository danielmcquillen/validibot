"""
E2E tests for the EnergyPlus parameterized template workflow.

These tests reproduce the exact scenario from the blog post
"Validating With EnergyPlus — Window Glazing Analysis":

1. A workflow accepts JSON parameter values (U_FACTOR, SHGC,
   VISIBLE_TRANSMITTANCE) for a parameterized IDF template.
2. Validibot substitutes the values into the template, runs a real
   EnergyPlus simulation in Docker, extracts output signals, and
   evaluates CEL assertions against the results.
3. The tests verify both passing and failing glazing scenarios, plus
   input validation (out-of-range values rejected before simulation).

Unlike the mocked tests in ``test_energyplus_e2e.py``, these run a
**real EnergyPlus simulation** in a Docker container against the local
Docker Compose stack.  Each test case takes 1-5 minutes depending on
the machine and whether the container image is cached.

Prerequisites:

- Docker Compose stack is running (``just local up`` or ``just local-cloud up --build``)
- ``setup_validibot`` has been run (creates EnergyPlus validator)
- ``seed_weather_files`` has been run (creates weather file records)
- ``validibot/validator-energyplus:latest`` Docker image is available
- Environment variables set (auto-provisioned by ``just local test-e2e-energyplus``)

Running::

    just local test-e2e-energyplus
"""

from __future__ import annotations

import logging

from tests.tests_e2e.helpers import assert_run_failed_assertion
from tests.tests_e2e.helpers import assert_run_failed_preprocessing
from tests.tests_e2e.helpers import assert_run_passed
from tests.tests_e2e.helpers import get_output_signal
from tests.tests_e2e.helpers import get_output_signals
from tests.tests_e2e.helpers import get_step_issues
from tests.tests_e2e.helpers import get_template_parameters_used
from tests.tests_e2e.helpers import submit_and_poll

logger = logging.getLogger(__name__)

# Assertion thresholds from the blog post.  These match the CEL assertions
# configured on the workflow by setup_e2e_workflows.
HEAT_LOSS_THRESHOLD_KWH = 800

# Sanity-check bounds for the passing scenario.  EnergyPlus results vary
# across versions so we assert a wide range rather than exact values.
HEAT_LOSS_MIN_SANITY_KWH = 50
HEAT_LOSS_FAIL_MIN_KWH = 700


class TestEnergyPlusParameterizedTemplate:
    """E2E tests for the EnergyPlus parameterized template workflow.

    Each test submits JSON parameter values via the API, waits for the
    EnergyPlus simulation to complete in Docker, and verifies the output
    signals and assertion results match expected behavior.

    Output value assertions use ranges rather than exact values because
    EnergyPlus results vary slightly across versions and platforms.
    """

    # ------------------------------------------------------------------
    # Test 1: Passing scenario (good double-pane window)
    # ------------------------------------------------------------------
    # Blog post values: U_FACTOR=1.70, SHGC=0.25, VT=0.42
    # Expected: both assertions pass (heat loss ~284 kWh, heating > cooling)
    # ------------------------------------------------------------------

    def test_passing_glazing_scenario(
        self,
        api_url,
        api_token,
        org_slug,
        energyplus_template_workflow_id,
    ):
        """A well-insulated window (U=1.70, SHGC=0.25) should pass all
        output assertions.

        This is the "good" scenario from the blog post: low U-Factor keeps
        heat in, low SHGC limits solar gain, so window heat loss stays well
        under 800 kWh and the building remains heating-dominated in San
        Francisco's mild climate.
        """
        payload = {
            "U_FACTOR": "1.70",
            "SHGC": "0.25",
            "VISIBLE_TRANSMITTANCE": "0.42",
        }

        result = submit_and_poll(
            api_url,
            api_token,
            org_slug,
            energyplus_template_workflow_id,
            payload,
        )

        # Run completed and all assertions passed
        assert_run_passed(result)

        # Output signals should be populated
        signals = get_output_signals(result)
        assert signals, "Expected output signals but got none"

        # Window heat loss should be well under the 800 kWh threshold.
        # Blog post shows ~284 kWh; we use a generous range to accommodate
        # variation across EnergyPlus versions and weather data.
        heat_loss = get_output_signal(result, "window_heat_loss_kwh")
        assert heat_loss is not None, "window_heat_loss_kwh signal not found in output"
        heat_loss_val = float(heat_loss)
        assert HEAT_LOSS_MIN_SANITY_KWH < heat_loss_val < HEAT_LOSS_THRESHOLD_KWH, (
            f"Expected window heat loss between {HEAT_LOSS_MIN_SANITY_KWH}-"
            f"{HEAT_LOSS_THRESHOLD_KWH} kWh, got {heat_loss_val}"
        )

        # Heating should exceed cooling (second assertion)
        heating = float(get_output_signal(result, "heating_energy_kwh") or 0)
        cooling = float(get_output_signal(result, "cooling_energy_kwh") or 0)
        assert heating > 0, f"Expected positive heating energy, got {heating}"
        assert cooling >= 0, f"Expected non-negative cooling energy, got {cooling}"
        assert heating > cooling, (
            f"Expected heating ({heating}) > cooling ({cooling}) "
            "for a low-SHGC window in a mild climate"
        )

        # Template parameters should be reported in the response
        params = get_template_parameters_used(result)
        assert params.get("U_FACTOR") == "1.70", (
            f"Expected U_FACTOR=1.70, got {params.get('U_FACTOR')}"
        )
        assert params.get("SHGC") == "0.25", (
            f"Expected SHGC=0.25, got {params.get('SHGC')}"
        )
        assert params.get("VISIBLE_TRANSMITTANCE") == "0.42", (
            f"Expected VT=0.42, got {params.get('VISIBLE_TRANSMITTANCE')}"
        )

        # Log all output signals for visibility
        logger.info("PASSED: Good double-pane window (U=1.70, SHGC=0.25, VT=0.42)")
        logger.info("  Output signals:")
        for slug, value in sorted(signals.items()):
            logger.info("    %-35s = %s", slug, value)
        logger.info(
            "  Verdict: heat_loss=%.1f kWh (< %d threshold), "
            "heating=%.1f kWh > cooling=%.1f kWh",
            heat_loss_val,
            HEAT_LOSS_THRESHOLD_KWH,
            heating,
            cooling,
        )

    # ------------------------------------------------------------------
    # Test 2: Failing scenario (poor single-pane window)
    # ------------------------------------------------------------------
    # Blog post values: U_FACTOR=6.00, SHGC=0.25, VT=0.42
    # Expected: simulation runs but heat loss assertion fails (~811 kWh > 800)
    # ------------------------------------------------------------------

    def test_failing_glazing_scenario(
        self,
        api_url,
        api_token,
        org_slug,
        energyplus_template_workflow_id,
    ):
        """A poorly-insulated window (U=6.00) should fail the heat loss
        assertion even though the simulation itself runs successfully.

        This is the "bad" scenario from the blog post: a single-pane
        uncoated window with U-Factor of 6.00 produces ~811 kWh of heat
        loss, exceeding the 800 kWh threshold.
        """
        payload = {
            "U_FACTOR": "6.00",
            "SHGC": "0.25",
            "VISIBLE_TRANSMITTANCE": "0.42",
        }

        result = submit_and_poll(
            api_url,
            api_token,
            org_slug,
            energyplus_template_workflow_id,
            payload,
        )

        # Simulation completed but assertion(s) failed
        assert_run_failed_assertion(result)

        # Window heat loss should exceed the 800 kWh threshold
        heat_loss = get_output_signal(result, "window_heat_loss_kwh")
        assert heat_loss is not None, "window_heat_loss_kwh signal not found in output"
        heat_loss_val = float(heat_loss)
        assert heat_loss_val > HEAT_LOSS_FAIL_MIN_KWH, (
            f"Expected high window heat loss (>{HEAT_LOSS_FAIL_MIN_KWH} kWh), "
            f"got {heat_loss_val}"
        )

        # There should be at least one ERROR issue from the assertion
        errors = get_step_issues(result, severity="ERROR")
        assert errors, "Expected ERROR issues from failed assertion"

        # The error message should reference the heat loss threshold
        error_messages = " ".join(e.get("message", "") for e in errors)
        assert "800" in error_messages, (
            f"Expected error message to mention '800' but got: {error_messages}"
        )

        # Template parameters should reflect the submitted values
        params = get_template_parameters_used(result)
        assert params.get("U_FACTOR") == "6.00", (
            f"Expected U_FACTOR=6.00, got {params.get('U_FACTOR')}"
        )

        # Log output signals and assertion errors for visibility
        signals = get_output_signals(result)
        logger.info(
            "FAILED (as expected): Poor single-pane window (U=6.00, SHGC=0.25, VT=0.42)"
        )
        logger.info("  Output signals:")
        for slug, value in sorted(signals.items()):
            logger.info("    %-35s = %s", slug, value)
        logger.info("  Assertion errors:")
        for err in errors:
            logger.info("    %s", err.get("message", "(no message)"))
        logger.info(
            "  Verdict: heat_loss=%.1f kWh (> %d threshold) — correctly rejected",
            heat_loss_val,
            HEAT_LOSS_THRESHOLD_KWH,
        )

    # ------------------------------------------------------------------
    # Test 3: Input validation (out-of-range value rejected)
    # ------------------------------------------------------------------
    # U_FACTOR=10.0 exceeds the max_value=7.0 constraint
    # Expected: run fails at preprocessing, no simulation started
    # ------------------------------------------------------------------

    def test_input_validation_rejects_out_of_range(
        self,
        api_url,
        api_token,
        org_slug,
        energyplus_template_workflow_id,
    ):
        """A U-Factor of 10.0 exceeds the author-defined maximum of 7.0
        and should be rejected at preprocessing — no simulation wasted.

        This demonstrates that template variable bounds are enforced
        before the EnergyPlus container is even launched, saving compute
        time and giving the submitter immediate, clear feedback.
        """
        payload = {
            "U_FACTOR": "10.0",
            "SHGC": "0.25",
            "VISIBLE_TRANSMITTANCE": "0.42",
        }

        result = submit_and_poll(
            api_url,
            api_token,
            org_slug,
            energyplus_template_workflow_id,
            payload,
        )

        # Run should fail at preprocessing (before simulation)
        assert_run_failed_preprocessing(result)

        # Error should mention the out-of-range parameter
        all_issues = get_step_issues(result, severity="ERROR") + get_step_issues(
            result,
            severity="WARNING",
        )

        # The error might be in step issues or in the run-level error field
        error_text = " ".join(e.get("message", "") for e in all_issues)
        run_error = result.get("data", {}).get("error", "") or ""
        user_error = result.get("data", {}).get("user_friendly_error", "") or ""
        combined = f"{error_text} {run_error} {user_error}"

        assert "U_FACTOR" in combined.upper() or "7.0" in combined, (
            f"Expected error to mention U_FACTOR or 7.0 but got: {combined}"
        )

        logger.info("REJECTED (as expected): Out-of-range U_FACTOR=10.0 (max 7.0)")
        if all_issues:
            logger.info("  Issues reported:")
            for issue in all_issues:
                logger.info(
                    "    [%s] %s",
                    issue.get("severity", "?"),
                    issue.get("message", "(no message)"),
                )
        if run_error:
            logger.info("  Run error: %s", run_error)
        if user_error:
            logger.info("  User error: %s", user_error)
        logger.info("  Verdict: rejected at preprocessing — no simulation wasted")
