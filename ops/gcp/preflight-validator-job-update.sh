#!/usr/bin/env bash
#
# Provider-side drain check for replacing a fixed-name validator Job image.
set -euo pipefail

PROJECT_ID=""
REGION=""
JOB_NAME=""
CURRENT_DIGEST=""
REPLACEMENT_DIGEST=""

die() {
    echo "Error: $*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --project)
            PROJECT_ID="${2:-}"
            shift 2
            ;;
        --project=*)
            PROJECT_ID="${1#*=}"
            shift
            ;;
        --region)
            REGION="${2:-}"
            shift 2
            ;;
        --region=*)
            REGION="${1#*=}"
            shift
            ;;
        --job)
            JOB_NAME="${2:-}"
            shift 2
            ;;
        --job=*)
            JOB_NAME="${1#*=}"
            shift
            ;;
        --current-digest)
            CURRENT_DIGEST="${2:-}"
            shift 2
            ;;
        --current-digest=*)
            CURRENT_DIGEST="${1#*=}"
            shift
            ;;
        --replacement-digest)
            REPLACEMENT_DIGEST="${2:-}"
            shift 2
            ;;
        --replacement-digest=*)
            REPLACEMENT_DIGEST="${1#*=}"
            shift
            ;;
        *)
            die "Unknown argument: $1"
            ;;
    esac
done

[ -n "$PROJECT_ID" ] || die "--project is required"
[ -n "$REGION" ] || die "--region is required"
[[ "$JOB_NAME" =~ ^[a-z][a-z0-9-]{0,62}$ ]] \
    || die "--job is not a valid Cloud Run Job name"
for digest in "$CURRENT_DIGEST" "$REPLACEMENT_DIGEST"; do
    [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] \
        || die "Both image digests must be lowercase sha256 values"
done
if [ "$CURRENT_DIGEST" = "$REPLACEMENT_DIGEST" ]; then
    echo "Fixed Job $JOB_NAME already uses $CURRENT_DIGEST."
    exit 0
fi

for command_name in gcloud jq mktemp; do
    command -v "$command_name" >/dev/null 2>&1 \
        || die "Required command not found: $command_name"
done

TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT
EXECUTIONS_JSON="$TEMP_ROOT/executions.json"

if ! gcloud run jobs executions list \
    --job="$JOB_NAME" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --format=json >"$EXECUTIONS_JSON"; then
    die "Could not inventory executions for fixed Job $JOB_NAME"
fi
jq -e 'type == "array"' "$EXECUTIONS_JSON" >/dev/null \
    || die "Invalid execution inventory for fixed Job $JOB_NAME"

# Cloud Run v2 exposes completionTime/completionStatus. The gcloud v1 shape
# exposes status.completionTime and a Completed condition. Treat any execution
# not positively terminal under either shape as active.
ACTIVE_EXECUTIONS="$(
    jq -r '
        def terminal:
          ((.completionTime // .status.completionTime // "") != "")
          or (
            (.completionStatus // "")
            | . == "EXECUTION_SUCCEEDED"
              or . == "EXECUTION_FAILED"
              or . == "EXECUTION_CANCELLED"
          )
          or any(
            (.conditions // .status.conditions // [])[];
            ((.type // "") == "Completed") and ((.status // "") == "True")
          );
        .[]
        | select(terminal | not)
        | (.metadata.name // .name // "<unknown-execution>")
        | split("/")
        | last
    ' "$EXECUTIONS_JSON"
)"
if [ -n "$ACTIVE_EXECUTIONS" ]; then
    echo "Error: fixed Job $JOB_NAME still has pending/running executions:" >&2
    while IFS= read -r execution_name; do
        [ -n "$execution_name" ] && echo "  - $execution_name" >&2
    done <<< "$ACTIVE_EXECUTIONS"
    exit 1
fi

echo "Fixed Job $JOB_NAME has no pending or running Cloud Run executions."
