#!/usr/bin/env bash
#
# Bound one Secret Manager secret's version history after a successful upload.
# Secret payloads are never accessed. Current Cloud Run service/job references
# are inventoried so numbered versions and aliases remain available.
set -euo pipefail

PROJECT_ID=""
SECRET_NAME=""
NEW_VERSION="latest"
KEEP_VERSIONS="2"
MODE="apply"

die() {
    echo "Error: $*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage:
  prune-secret-versions.sh --project PROJECT --secret SECRET \
      [--new-version VERSION] [--keep COUNT] [--mode preview|apply]

Keeps the newly established latest version, the newest COUNT non-destroyed
versions, and every version referenced by a current Cloud Run service or job.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --project)
            [ "$#" -ge 2 ] || die "--project requires a value"
            PROJECT_ID="$2"
            shift 2
            ;;
        --secret)
            [ "$#" -ge 2 ] || die "--secret requires a value"
            SECRET_NAME="$2"
            shift 2
            ;;
        --new-version)
            [ "$#" -ge 2 ] || die "--new-version requires a value"
            NEW_VERSION="$2"
            shift 2
            ;;
        --keep)
            [ "$#" -ge 2 ] || die "--keep requires a value"
            KEEP_VERSIONS="$2"
            shift 2
            ;;
        --mode)
            [ "$#" -ge 2 ] || die "--mode requires a value"
            MODE="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            die "Unknown argument: $1"
            ;;
    esac
done

[ -n "$PROJECT_ID" ] || die "--project is required"
[ -n "$SECRET_NAME" ] || die "--secret is required"
[[ "$KEEP_VERSIONS" =~ ^[1-9][0-9]*$ ]] \
    || die "--keep must be a positive integer"
case "$MODE" in
    preview|apply) ;;
    *) die "--mode must be preview or apply" ;;
esac

for command_name in gcloud jq awk sort mktemp; do
    command -v "$command_name" >/dev/null 2>&1 \
        || die "Required command not found: $command_name"
done

TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEMP_ROOT"' EXIT

SERVICES_JSON="$TEMP_ROOT/services.json"
JOBS_JSON="$TEMP_ROOT/jobs.json"
REFERENCE_LABELS="$TEMP_ROOT/references.tsv"
PROTECTED_VERSIONS="$TEMP_ROOT/protected.txt"
FRESH_REFERENCE_LABELS="$TEMP_ROOT/fresh-references.tsv"
FRESH_PROTECTED_VERSIONS="$TEMP_ROOT/fresh-protected.txt"
VERSIONS_JSON="$TEMP_ROOT/versions.json"
CANDIDATES="$TEMP_ROOT/candidates.tsv"

inventory_cloud_run_references() {
    local services_json="$1"
    local jobs_json="$2"
    local references_tsv="$3"

    if ! gcloud run services list \
        --project="$PROJECT_ID" \
        --platform=managed \
        --format=json >"$services_json"; then
        die "Could not inventory Cloud Run services; refusing to prune $SECRET_NAME"
    fi
    if ! gcloud run jobs list \
        --project="$PROJECT_ID" \
        --format=json >"$jobs_json"; then
        die "Could not inventory Cloud Run jobs; refusing to prune $SECRET_NAME"
    fi
    jq -e 'type == "array"' "$services_json" >/dev/null \
        || die "Invalid Cloud Run service inventory; refusing to prune $SECRET_NAME"
    jq -e 'type == "array"' "$jobs_json" >/dev/null \
        || die "Invalid Cloud Run job inventory; refusing to prune $SECRET_NAME"

    # Handles the Cloud Run v1 and v2 shapes for secret volumes and
    # secret-backed environment variables. Only reference metadata is read.
    jq -r '
        def secret_basename:
          tostring
          | if contains("/") then split("/") | last else . end;
        (
          .. | objects
          | select(
              ((.secretName? // "") != "")
              and ((.items? // null) | type == "array")
            )
          | (.secretName | secret_basename) as $secret
          | .items[]?
          | [$secret, (.key // .version // "latest" | tostring)]
        ),
        (
          .. | objects
          | select(
              ((.secret? // null) | type == "string")
              and ((.items? // null) | type == "array")
            )
          | (.secret | secret_basename) as $secret
          | .items[]?
          | [$secret, (.version // .key // "latest" | tostring)]
        ),
        (
          .. | objects
          | .secretKeyRef? // empty
          | [
              ((.name // .secret // "") | secret_basename),
              (.key // .version // "latest" | tostring)
            ]
          | select(.[0] != "")
        )
        | @tsv
    ' "$services_json" "$jobs_json" | sort -u >"$references_tsv"
}

resolve_enabled_version() {
    local version_label="$1"
    local description_json
    local state
    local resolved_version

    description_json="$(mktemp "$TEMP_ROOT/version-description.XXXXXX")"
    if ! gcloud secrets versions describe "$version_label" \
        --secret="$SECRET_NAME" \
        --project="$PROJECT_ID" \
        --format=json >"$description_json"; then
        die "$SECRET_NAME version reference '$version_label' cannot be resolved"
    fi
    state="$(jq -r '.state // "MISSING"' "$description_json")"
    resolved_version="$(
        jq -r '.name // "" | split("/") | last' "$description_json"
    )"
    [ "$state" = "ENABLED" ] \
        || die "$SECRET_NAME version reference '$version_label' is $state, not ENABLED"
    [[ "$resolved_version" =~ ^[1-9][0-9]*$ ]] \
        || die "$SECRET_NAME version reference '$version_label' resolved to an invalid version"
    printf '%s\n' "$resolved_version"
}

collect_protected_versions() {
    local references_tsv="$1"
    local protected_versions="$2"
    local referenced_secret
    local version_label
    local resolved_version

    : >"$protected_versions"
    printf '%s\n' "$ESTABLISHED_VERSION" >>"$protected_versions"
    while IFS=$'\t' read -r referenced_secret version_label; do
        [ "$referenced_secret" = "$SECRET_NAME" ] || continue
        [ -n "$version_label" ] || version_label="latest"
        resolved_version="$(resolve_enabled_version "$version_label")"
        printf '%s\n' "$resolved_version" >>"$protected_versions"
    done <"$references_tsv"
    sort -u -o "$protected_versions" "$protected_versions"
}

inventory_cloud_run_references \
    "$SERVICES_JSON" "$JOBS_JSON" "$REFERENCE_LABELS"

if ! gcloud secrets versions list "$SECRET_NAME" \
    --project="$PROJECT_ID" \
    --format=json >"$VERSIONS_JSON"; then
    die "Could not inventory versions for $SECRET_NAME; refusing to prune"
fi
if ! jq -e '
    type == "array"
    and all(.[];
      (.name | type == "string")
      and (
        .state == "ENABLED"
        or .state == "DISABLED"
        or .state == "DESTROYED"
      )
      and (.createTime | type == "string")
    )
' "$VERSIONS_JSON" >/dev/null; then
    die "Invalid version inventory for $SECRET_NAME; refusing to prune"
fi

ESTABLISHED_VERSION="$(resolve_enabled_version "$NEW_VERSION")"
LATEST_VERSION="$(resolve_enabled_version latest)"
[ "$LATEST_VERSION" = "$ESTABLISHED_VERSION" ] \
    || die "$SECRET_NAME latest is version $LATEST_VERSION, not newly established version $ESTABLISHED_VERSION"

INVENTORY_STATE="$(
    jq -r --arg version "$ESTABLISHED_VERSION" '
        [
          .[]
          | select((.name | split("/") | last) == $version)
          | .state
        ][0] // "MISSING"
    ' "$VERSIONS_JSON"
)"
[ "$INVENTORY_STATE" = "ENABLED" ] \
    || die "$SECRET_NAME version $ESTABLISHED_VERSION is $INVENTORY_STATE in the version inventory"

collect_protected_versions "$REFERENCE_LABELS" "$PROTECTED_VERSIONS"
: >"$CANDIDATES"

NON_DESTROYED_INDEX=0
while IFS=$'\t' read -r version state created; do
    [ -n "$version" ] || continue
    NON_DESTROYED_INDEX=$((NON_DESTROYED_INDEX + 1))
    if [ "$NON_DESTROYED_INDEX" -le "$KEEP_VERSIONS" ]; then
        echo "KEEP    $SECRET_NAME version $version ($state; recent)"
    elif awk -v version="$version" '$0 == version { found = 1 } END { exit(found ? 0 : 1) }' \
        "$PROTECTED_VERSIONS"; then
        echo "KEEP    $SECRET_NAME version $version ($state; referenced by Cloud Run)"
    else
        echo "DESTROY $SECRET_NAME version $version ($state; created $created)"
        printf '%s\t%s\t%s\n' "$version" "$state" "$created" >>"$CANDIDATES"
    fi
done < <(
    jq -r '
        [
          .[]
          | select(.state != "DESTROYED")
        ]
        | sort_by(.createTime)
        | reverse
        | .[]
        | [
            (.name | split("/") | last),
            .state,
            .createTime
          ]
        | @tsv
    ' "$VERSIONS_JSON"
)

CANDIDATE_COUNT="$(awk 'END { print NR + 0 }' "$CANDIDATES")"
if [ "$CANDIDATE_COUNT" -eq 0 ]; then
    echo "Secret version retention is already satisfied for $SECRET_NAME."
    exit 0
fi
if [ "$MODE" = "preview" ]; then
    echo "Preview only: $CANDIDATE_COUNT version(s) would be destroyed."
    exit 0
fi

# Re-inventory immediately before mutation. This prevents a deployment that
# appeared during planning from turning a destruction candidate into a pin.
inventory_cloud_run_references \
    "$TEMP_ROOT/fresh-services.json" \
    "$TEMP_ROOT/fresh-jobs.json" \
    "$FRESH_REFERENCE_LABELS"
collect_protected_versions \
    "$FRESH_REFERENCE_LABELS" "$FRESH_PROTECTED_VERSIONS"

while IFS=$'\t' read -r version planned_state _created; do
    if awk -v version="$version" '$0 == version { found = 1 } END { exit(found ? 0 : 1) }' \
        "$FRESH_PROTECTED_VERSIONS"; then
        die "$SECRET_NAME version $version became referenced; no versions were destroyed"
    fi

    current_json="$(mktemp "$TEMP_ROOT/current-version.XXXXXX")"
    if ! gcloud secrets versions describe "$version" \
        --secret="$SECRET_NAME" \
        --project="$PROJECT_ID" \
        --format=json >"$current_json"; then
        die "Could not re-check $SECRET_NAME version $version; no versions were destroyed"
    fi
    current_state="$(jq -r '.state // "MISSING"' "$current_json")"
    [ "$current_state" = "$planned_state" ] \
        || die "$SECRET_NAME version $version changed from $planned_state to $current_state; no versions were destroyed"
done <"$CANDIDATES"

LATEST_VERSION="$(resolve_enabled_version latest)"
[ "$LATEST_VERSION" = "$ESTABLISHED_VERSION" ] \
    || die "$SECRET_NAME latest changed before pruning; no versions were destroyed"

while IFS=$'\t' read -r version _state _created; do
    gcloud secrets versions destroy "$version" \
        --secret="$SECRET_NAME" \
        --project="$PROJECT_ID" \
        --quiet
done <"$CANDIDATES"

echo "Destroyed $CANDIDATE_COUNT old version(s) of $SECRET_NAME."
