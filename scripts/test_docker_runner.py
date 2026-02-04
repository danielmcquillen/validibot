#!/usr/bin/env python
"""
Manual test script for the Docker validator runner.

This script tests the Docker runner end-to-end by:
1. Creating a test input envelope with file:// URIs
2. Running the EnergyPlus validator container
3. Verifying the output envelope was created

Usage:
    source set-env.sh  # Load environment
    uv run python scripts/test_docker_runner.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import uuid
from pathlib import Path

# Setup Django before importing Django modules
import django

sys.path.insert(0, str(Path(__file__).parent.parent))
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()


from validibot.core.storage import get_data_storage
from validibot.validations.services.runners import get_validator_runner


def create_test_envelope(
    storage,
    base_path: str,
    submission_content: str,
    weather_content: bytes,
) -> dict:
    """Create a minimal EnergyPlus input envelope for testing."""
    # Upload the submission file
    submission_path = f"{base_path}/model.epjson"
    storage.write(submission_path, submission_content)
    submission_uri = storage.get_uri(submission_path)

    # Upload the weather file
    weather_path = f"{base_path}/weather.epw"
    storage.write(weather_path, weather_content)
    weather_uri = storage.get_uri(weather_path)

    run_id = str(uuid.uuid4())
    validator_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    workflow_id = str(uuid.uuid4())
    step_id = str(uuid.uuid4())

    # Build a properly formed input envelope matching EnergyPlusInputEnvelope schema
    envelope = {
        "schema_version": "validibot.input.v1",
        "run_id": run_id,
        "validator": {
            "id": validator_id,
            "type": "ENERGYPLUS",
            "version": "1.0.0",
        },
        "org": {
            "id": org_id,
            "name": "Test Organization",
        },
        "workflow": {
            "id": workflow_id,
            "step_id": step_id,
            "step_name": "EnergyPlus Validation Test",
        },
        "context": {
            "callback_url": "http://localhost:8000/api/v1/validation-callbacks/",
            "callback_id": "test-callback-id",
            "execution_bundle_uri": storage.get_uri(base_path),
            "skip_callback": True,  # Skip callback for test (sync mode)
            "timeout_seconds": 600,
            "tags": ["test"],
        },
        "input_files": [
            {
                "name": "model.epjson",
                "mime_type": "application/vnd.energyplus.epjson",
                "role": "primary-model",
                "uri": submission_uri,
            },
            {
                "name": "weather.epw",
                "mime_type": "application/vnd.energyplus.epw",
                "role": "weather",
                "uri": weather_uri,
            },
        ],
        # EnergyPlusInputs - typed configuration
        "inputs": {
            "timestep_per_hour": 4,
            "invocation_mode": "cli",
        },
    }

    return envelope


def main():
    print("=" * 60)
    print("Docker Runner End-to-End Test")
    print("=" * 60)

    # 1. Check runner availability
    print("\n1. Checking Docker runner availability...")
    runner = get_validator_runner()
    runner_type = runner.get_runner_type()
    print(f"   Runner type: {runner_type}")

    if runner_type != "docker":
        print(f"   ERROR: Expected 'docker' runner, got '{runner_type}'")
        print("   Make sure VALIDATOR_RUNNER=docker in your environment")
        sys.exit(1)

    if not runner.is_available():
        print("   ERROR: Docker is not available")
        print("   Make sure Docker Desktop is running")
        sys.exit(1)

    print("   Docker is available!")

    # 2. Get storage
    print("\n2. Setting up storage...")
    storage = get_data_storage()
    storage_type = type(storage).__name__
    print(f"   Storage type: {storage_type}")

    # 3. Create test data
    print("\n3. Creating test data...")
    test_id = str(uuid.uuid4())[:8]
    base_path = f"test-runs/{test_id}"

    # Load the example EnergyPlus JSON
    example_path = Path(__file__).parent.parent / "tests" / "data" / "energyplus" / "example_epjson.json"
    if not example_path.exists():
        print(f"   ERROR: Example file not found: {example_path}")
        sys.exit(1)

    submission_content = example_path.read_text()
    print(f"   Loaded example model ({len(submission_content)} bytes)")

    # Load the weather file
    weather_path = Path(__file__).parent.parent / "tests" / "data" / "energyplus" / "test_weather.epw"
    if not weather_path.exists():
        print(f"   ERROR: Weather file not found: {weather_path}")
        sys.exit(1)

    weather_content = weather_path.read_bytes()
    print(f"   Loaded weather file ({len(weather_content)} bytes)")

    # Create and upload envelope
    envelope = create_test_envelope(storage, base_path, submission_content, weather_content)
    input_envelope_path = f"{base_path}/input.json"
    storage.write(input_envelope_path, json.dumps(envelope, indent=2))
    input_uri = storage.get_uri(input_envelope_path)
    print(f"   Input envelope: {input_uri}")

    output_envelope_path = f"{base_path}/output.json"
    output_uri = storage.get_uri(output_envelope_path)
    print(f"   Expected output: {output_uri}")

    # 4. Run container
    print("\n4. Running EnergyPlus validator container...")
    container_image = "validibot-validator-energyplus:latest"
    print(f"   Image: {container_image}")
    print(f"   Input URI: {input_uri}")
    print(f"   Output URI: {output_uri}")
    print("   (this may take a moment...)")

    try:
        result = runner.run(
            container_image=container_image,
            input_uri=input_uri,
            output_uri=output_uri,
            timeout_seconds=120,  # 2 minutes for test
        )
    except Exception as e:
        print(f"\n   ERROR: Container failed to run: {e}")
        sys.exit(1)

    # 5. Check result
    print("\n5. Container execution result:")
    print(f"   Execution ID: {result.execution_id}")
    print(f"   Exit code: {result.exit_code}")
    print(f"   Succeeded: {result.succeeded}")
    print(f"   Duration: {result.duration_seconds:.2f}s")

    if result.error_message:
        print(f"   Error: {result.error_message}")

    if result.logs:
        print("\n   Container logs (last 50 lines):")
        log_lines = result.logs.strip().split("\n")[-50:]
        for line in log_lines:
            print(f"   | {line}")

    # 6. Check output envelope
    print("\n6. Checking output envelope...")
    if storage.exists(output_envelope_path):
        output_data = storage.read_text(output_envelope_path)
        output_envelope = json.loads(output_data)
        print(f"   Status: {output_envelope.get('status', 'unknown')}")
        print(f"   Run ID: {output_envelope.get('run_id', 'unknown')}")

        timing = output_envelope.get("timing", {})
        if timing:
            print(f"   Timing: {timing.get('total_seconds', 'N/A')}s")

        messages = output_envelope.get("messages", [])
        if messages:
            print(f"   Messages ({len(messages)}):")
            for msg in messages[:5]:
                print(f"     - [{msg.get('severity', '?')}] {msg.get('message', '')[:60]}")
            if len(messages) > 5:
                print(f"     ... and {len(messages) - 5} more")
    else:
        print("   ERROR: Output envelope not found!")
        print(f"   Expected at: {output_uri}")

    # 7. Summary
    print("\n" + "=" * 60)
    if result.succeeded:
        print("TEST PASSED: Docker runner executed successfully!")
    else:
        print("TEST FAILED: Container did not complete successfully")
    print("=" * 60)

    # Cleanup option
    print(f"\nTest files at: {storage.get_uri(base_path)}")
    print("Run storage.delete_prefix('{base_path}') to clean up")


if __name__ == "__main__":
    main()
