"""Enforce a ratcheting baseline for production-code mypy errors.

The project adopted type checking after substantial Django code already
existed. Treating every historical diagnostic as immediately fatal would make
CI unusable; ignoring mypy entirely lets the debt grow. This checker records
the allowed count for each ``file + error-code`` pair. Existing counts may
decrease, while a new pair or any count increase fails.

Run ``uv run python scripts/check_mypy_baseline.py`` for the CI-equivalent
check. After intentionally fixing existing diagnostics, run the same command
with ``--update`` to ratchet the committed baseline downward.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

SCHEMA_VERSION = 1
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).with_name("mypy_baseline.json")
MYPY_ARGUMENTS = (
    "validibot",
    "--exclude",
    r"(^|/)(tests|migrations)/",
)
LOCATION_PATTERN = re.compile(r"^(?P<path>.+?):\d+(?::\d+)?$")
ERROR_CODE_PATTERN = re.compile(r"\s+\[(?P<code>[a-z0-9-]+)\]$")


def parse_mypy_errors(output: str) -> Counter[str]:
    """Return normalized ``path|code`` counts from mypy's text output."""
    errors: Counter[str] = Counter()
    for line in output.splitlines():
        if ": error: " not in line:
            continue
        location, message = line.split(": error: ", 1)
        match = LOCATION_PATTERN.match(location)
        if match is None:
            continue
        path = Path(match.group("path"))
        try:
            normalized_path = path.resolve().relative_to(REPOSITORY_ROOT).as_posix()
        except ValueError:
            normalized_path = path.as_posix()
        code_match = ERROR_CODE_PATTERN.search(message)
        code = code_match.group("code") if code_match else "uncoded"
        errors[f"{normalized_path}|{code}"] += 1
    return errors


def baseline_regressions(
    *,
    current: Counter[str],
    allowed: Counter[str],
) -> dict[str, tuple[int, int]]:
    """Return diagnostics whose current count exceeds the committed allowance."""
    return {
        key: (allowed.get(key, 0), count)
        for key, count in sorted(current.items())
        if count > allowed.get(key, 0)
    }


def _load_baseline() -> Counter[str]:
    """Load and validate the committed baseline document."""
    try:
        payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(
            "Mypy baseline is missing; run this script with --update."
        ) from exc
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit("Unsupported mypy baseline schema version.")
    allowed = payload.get("allowed_errors")
    if not isinstance(allowed, dict) or not all(
        isinstance(key, str) and isinstance(value, int)
        for key, value in allowed.items()
    ):
        raise SystemExit("Mypy baseline has invalid allowed_errors data.")
    return Counter(allowed)


def _write_baseline(errors: Counter[str]) -> None:
    """Write deterministic counts after an intentional baseline update."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "description": (
            "Maximum production mypy diagnostics by relative file and error code. "
            "Counts may decrease without updating this file; increases fail CI."
        ),
        "allowed_errors": dict(sorted(errors.items())),
    }
    BASELINE_PATH.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_mypy() -> tuple[int, str, Counter[str]]:
    """Execute lockfile-pinned mypy and return its normalized diagnostics."""
    completed = subprocess.run(  # noqa: S603 - fixed interpreter/module/arguments
        [sys.executable, "-m", "mypy", *MYPY_ARGUMENTS],
        cwd=REPOSITORY_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return completed.returncode, completed.stdout, parse_mypy_errors(completed.stdout)


def main() -> int:
    """Update the baseline or fail when production type debt increases."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Replace the baseline with the current diagnostic counts.",
    )
    options = parser.parse_args()

    returncode, output, current = _run_mypy()
    if returncode not in {0, 1}:
        sys.stderr.write(output)
        sys.stderr.write(f"mypy failed unexpectedly with exit code {returncode}.\n")
        return returncode or 2
    if returncode == 1 and not current:
        sys.stderr.write(output)
        sys.stderr.write("mypy failed but no diagnostics could be normalized.\n")
        return 2

    if options.update:
        _write_baseline(current)
        sys.stdout.write(
            f"Updated mypy baseline with {sum(current.values())} diagnostics.\n"
        )
        return 0

    allowed = _load_baseline()
    regressions = baseline_regressions(current=current, allowed=allowed)
    if regressions:
        sys.stderr.write("Mypy baseline regression:\n")
        for key, (old_count, new_count) in regressions.items():
            sys.stderr.write(f"  {key}: allowed {old_count}, found {new_count}\n")
        sys.stderr.write(
            "Fix the new diagnostics. Use --update only for an intentional "
            "baseline decision.\n"
        )
        return 1

    current_total = sum(current.values())
    allowed_total = sum(allowed.values())
    sys.stdout.write(
        "Mypy baseline passed: "
        f"{current_total} current diagnostics, {allowed_total} allowed.\n"
    )
    if current_total < allowed_total:
        sys.stdout.write(
            "Type debt decreased; run with --update to ratchet the baseline.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
