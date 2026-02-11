# ruff: noqa: INP001, T201
"""
Validibot API Client Example

This script demonstrates how to use the Validibot API to:
1. Authenticate with an API key
2. Submit a file for validation
3. Poll for results
4. Display validation findings

Prerequisites:
    pip install requests

Usage:
    # Set your API key (get it from Settings -> API Key in the web UI)
    export VALIDIBOT_API_KEY="your-api-key-here"

    # Run the script
    python example-client.py --org my-org --workflow my-workflow --file model.idf

    # You can also use workflow ID instead of slug
    python example-client.py --org my-org --workflow 123 --file model.idf
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
API_KEY = os.getenv("VALIDIBOT_API_KEY", "")

FINDINGS_PREVIEW_LIMIT = 10


def get_auth_headers() -> dict:
    """Get authentication headers for API requests."""
    if not API_KEY:
        print("Error: VALIDIBOT_API_KEY environment variable not set")
        print("Get your API key from: Settings -> API Key in the web UI")
        sys.exit(1)
    return {"Authorization": f"Bearer {API_KEY}"}


def submit_validation(
    org_slug: str,
    workflow: str,
    file_path: Path,
    *,
    name: str | None = None,
) -> dict:
    """
    Submit a file for validation.

    Args:
        org_slug: Organization slug (e.g., "my-org")
        workflow: Workflow slug or numeric ID
        file_path: Path to the file to validate
        name: Optional name for the submission

    Returns:
        API response with run details including id
    """
    headers = get_auth_headers()

    with file_path.open("rb") as f:
        files = {"file": (file_path.name, f)}
        data = {}
        if name:
            data["name"] = name

        response = requests.post(
            f"{API_BASE_URL}/orgs/{org_slug}/workflows/{workflow}/runs/",
            headers=headers,
            files=files,
            data=data,
            timeout=60,
        )

    response.raise_for_status()
    return response.json()


def get_run_status(org_slug: str, run_id: str) -> dict:
    """
    Get the status and results of a validation run.

    Args:
        org_slug: Organization slug
        run_id: UUID of the validation run

    Returns:
        Run details including status, steps, and findings
    """
    headers = get_auth_headers()
    response = requests.get(
        f"{API_BASE_URL}/orgs/{org_slug}/runs/{run_id}/",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def wait_for_completion(
    org_slug: str,
    run_id: str,
    *,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> dict:
    """
    Poll until the validation run completes.

    Args:
        org_slug: Organization slug
        run_id: UUID of the validation run
        timeout_seconds: Maximum time to wait
        poll_interval: Seconds between polls

    Returns:
        Final run details including results
    """
    terminal_statuses = {"SUCCEEDED", "FAILED", "CANCELED", "TIMED_OUT"}
    start_time = time.time()

    while time.time() - start_time < timeout_seconds:
        run_data = get_run_status(org_slug, run_id)
        run_status = run_data.get("status", "UNKNOWN")

        print(f"  Status: {run_status}")

        if run_status in terminal_statuses:
            return run_data

        time.sleep(poll_interval)

    raise TimeoutError(f"Run {run_id} did not complete within {timeout_seconds}s")


def extract_findings(run_data: dict) -> list[dict]:
    """
    Extract findings from run data.

    Findings are nested in the steps array of the run response.

    Args:
        run_data: Run response from the API

    Returns:
        Flattened list of all findings across steps
    """
    findings = []
    for step in run_data.get("steps", []):
        step_findings = step.get("findings", [])
        findings.extend(step_findings)
    return findings


def main():
    parser = argparse.ArgumentParser(
        description="Submit a file to Validibot for validation"
    )
    parser.add_argument(
        "--org",
        "-o",
        required=True,
        help="Organization slug (e.g., 'my-org')",
    )
    parser.add_argument(
        "--workflow",
        "-w",
        required=True,
        help="Workflow slug or numeric ID",
    )
    parser.add_argument(
        "--file",
        "-f",
        required=True,
        type=Path,
        help="Path to the file to validate",
    )
    parser.add_argument(
        "--name",
        "-n",
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

    print(
        f"Submitting {args.file.name} to workflow"
        f" '{args.workflow}' in org '{args.org}'..."
    )

    # Submit the file
    result = submit_validation(
        args.org,
        args.workflow,
        args.file,
        name=args.name,
    )

    run_id = result.get("id")
    if not run_id:
        print(f"Error: No id in response: {result}")
        sys.exit(1)

    print(f"Created validation run: {run_id}")

    if args.no_wait:
        print(f"Check status at: {API_BASE_URL}/orgs/{args.org}/runs/{run_id}/")
        return

    # Wait for completion
    print("Waiting for completion...")
    try:
        final_data = wait_for_completion(
            args.org,
            run_id,
            timeout_seconds=args.timeout,
        )
    except TimeoutError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Print results
    status = final_data.get("status")
    print(f"\nValidation {status}")

    if status == "FAILED":
        error_category = final_data.get("error_category", "")
        error_message = final_data.get("user_friendly_error", "")
        if error_category:
            print(f"  Error category: {error_category}")
        if error_message:
            print(f"  Message: {error_message}")

    # Get findings from the run data (findings are in the steps array)
    if status in ("SUCCEEDED", "FAILED"):
        findings = extract_findings(final_data)
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
