# ruff: noqa: INP001, T201
"""
Validibot API Client Example

This script demonstrates how to use the Validibot API to:
1. Authenticate with an API token
2. Submit a file for validation
3. Poll for results
4. Retrieve validation findings

Prerequisites:
    pip install requests

Usage:
    # Set your API token
    export VALIDIBOT_API_TOKEN="your-token-here"

    # Run the script
    python example-client.py --file model.idf --workflow workflow-uuid
"""

import argparse
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: requests library required. Install with: pip install requests")
    sys.exit(1)


# Configuration
API_BASE_URL = os.getenv("VALIDIBOT_API_URL", "https://app.validibot.com/api/v1")
API_TOKEN = os.getenv("VALIDIBOT_API_TOKEN", "")

FINDINGS_PREVIEW_LIMIT = 10


def get_auth_headers() -> dict:
    """Get authentication headers for API requests."""
    if not API_TOKEN:
        print("Error: VALIDIBOT_API_TOKEN environment variable not set")
        print("Get your API key from: Settings -> API Key")
        sys.exit(1)
    return {"Authorization": f"Bearer {API_TOKEN}"}


def get_token(username: str, password: str) -> str:
    """
    Get an API token using username/password.

    Use this if you need to obtain a token programmatically.
    Alternatively, create an API key in the web UI under Settings -> API Key.
    """
    response = requests.post(
        f"{API_BASE_URL}/auth-token/",
        json={"username": username, "password": password},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["token"]


def submit_validation(
    file_path: Path,
    workflow_id: str,
    *,
    name: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """
    Submit a file for validation.

    Args:
        file_path: Path to the file to validate
        workflow_id: UUID of the workflow to run
        name: Optional name for the submission
        metadata: Optional metadata dict

    Returns:
        API response with run_id
    """
    headers = get_auth_headers()

    with file_path.open("rb") as f:
        files = {"file": (file_path.name, f)}
        data = {
            "workflow_id": workflow_id,
        }
        if name:
            data["name"] = name
        if metadata:
            import json
            data["metadata"] = json.dumps(metadata)

        response = requests.post(
            f"{API_BASE_URL}/validations/submit/",
            headers=headers,
            files=files,
            data=data,
            timeout=60,
        )

    response.raise_for_status()
    return response.json()


def get_run_status(run_id: str) -> dict:
    """
    Get the status of a validation run.

    Args:
        run_id: UUID of the validation run

    Returns:
        Run status including status, error_category if failed
    """
    headers = get_auth_headers()
    response = requests.get(
        f"{API_BASE_URL}/validations/runs/{run_id}/",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def get_findings(run_id: str) -> list[dict]:
    """
    Get validation findings for a completed run.

    Args:
        run_id: UUID of the validation run

    Returns:
        List of findings with severity, message, path, etc.
    """
    headers = get_auth_headers()
    response = requests.get(
        f"{API_BASE_URL}/validations/runs/{run_id}/findings/",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def wait_for_completion(
    run_id: str,
    *,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> dict:
    """
    Poll until the validation run completes.

    Args:
        run_id: UUID of the validation run
        timeout_seconds: Maximum time to wait
        poll_interval: Seconds between polls

    Returns:
        Final run status
    """
    terminal_statuses = {"SUCCEEDED", "FAILED", "CANCELED", "TIMED_OUT"}
    start_time = time.time()

    while time.time() - start_time < timeout_seconds:
        status = get_run_status(run_id)
        run_status = status.get("status", "UNKNOWN")

        print(f"  Status: {run_status}")

        if run_status in terminal_statuses:
            return status

        time.sleep(poll_interval)

    raise TimeoutError(f"Run {run_id} did not complete within {timeout_seconds}s")


def main():
    parser = argparse.ArgumentParser(
        description="Submit a file to Validibot for validation"
    )
    parser.add_argument(
        "--file", "-f",
        required=True,
        type=Path,
        help="Path to the file to validate",
    )
    parser.add_argument(
        "--workflow", "-w",
        required=True,
        help="UUID of the workflow to run",
    )
    parser.add_argument(
        "--name", "-n",
        help="Optional name for the submission",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Don't wait for completion, just submit and exit",
    )

    args = parser.parse_args()

    if not args.file.exists():
        print(f"Error: File not found: {args.file}")
        sys.exit(1)

    print(f"Submitting {args.file.name} to workflow {args.workflow}...")

    # Submit the file
    result = submit_validation(
        args.file,
        args.workflow,
        name=args.name,
    )

    run_id = result.get("run_id")
    if not run_id:
        print(f"Error: No run_id in response: {result}")
        sys.exit(1)

    print(f"Created validation run: {run_id}")

    if args.no_wait:
        print(f"Check status at: {API_BASE_URL}/validations/runs/{run_id}/")
        return

    # Wait for completion
    print("Waiting for completion...")
    try:
        final_status = wait_for_completion(
            run_id,
            timeout_seconds=args.timeout,
        )
    except TimeoutError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Print results
    status = final_status.get("status")
    print(f"\nValidation {status}")

    if status == "FAILED":
        error_category = final_status.get("error_category", "")
        error_message = final_status.get("user_friendly_error", "")
        if error_category:
            print(f"  Error category: {error_category}")
        if error_message:
            print(f"  Message: {error_message}")

    # Get findings
    if status in ("SUCCEEDED", "FAILED"):
        findings = get_findings(run_id)
        if findings:
            print(f"\nFindings ({len(findings)}):")
            for f in findings[:FINDINGS_PREVIEW_LIMIT]:  # Show first N
                severity = f.get("severity", "?")
                message = f.get("message", "")[:80]
                path = f.get("path", "")
                print(f"  [{severity}] {message}")
                if path:
                    print(f"         at: {path}")
            if len(findings) > FINDINGS_PREVIEW_LIMIT:
                print(f"  ... and {len(findings) - FINDINGS_PREVIEW_LIMIT} more")
        else:
            print("\nNo findings.")

    # Exit with appropriate code
    if status == "SUCCEEDED":
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
