#!/usr/bin/env bash
# Validate and load one CI-produced release-image artifact without rebuilding
# any application layer. Staging and production use the same verifier so the
# immutable artifact's integrity and provenance checks cannot drift.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/load-verified-release-images.sh <commit-sha> [options]

Options:
  --artifact-dir <path>             Required directory downloaded from GitHub Actions
  --repository <owner/repository>   Required repository recorded in artifact metadata
  --ci-run-id <id>                  Required CI workflow run ID
  --ci-run-attempt <number>         Required CI workflow run attempt
  --expected-api-image-id <sha256>  Optional completed-staging API image ID
  --expected-caddy-image-id <sha256>
                                    Optional completed-staging Caddy image ID
  --github-output <path>            Optional GitHub output file for loaded image IDs

The artifact must contain the exact archive, checksum and metadata generated
by Continuous integration. This command verifies all three before loading the
API and Caddy images, then verifies their immutable revision and CI labels.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

metadata_value() {
  local metadata="$1" key="$2" value count
  count="$(grep -c "^${key}=" "$metadata" || true)"
  [[ "$count" == "1" ]] || die "Release image metadata is missing or repeats '$key'."
  value="$(sed -n "s/^${key}=//p" "$metadata")"
  [[ -n "$value" ]] || die "Release image metadata has an empty '$key'."
  printf '%s' "$value"
}

image_label() {
  local image="$1" label="$2"
  docker image inspect --format "{{ index .Config.Labels \"$label\" }}" "$image"
}

release_commit="${1:-}"
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || { usage >&2; exit 1; }
shift || true

artifact_dir=""
repository=""
ci_run_id=""
ci_run_attempt=""
expected_api_image_id=""
expected_caddy_image_id=""
github_output=""
while (($#)); do
  case "$1" in
    --artifact-dir) artifact_dir="${2:?--artifact-dir requires a value}"; shift 2 ;;
    --repository) repository="${2:?--repository requires a value}"; shift 2 ;;
    --ci-run-id) ci_run_id="${2:?--ci-run-id requires a value}"; shift 2 ;;
    --ci-run-attempt) ci_run_attempt="${2:?--ci-run-attempt requires a value}"; shift 2 ;;
    --expected-api-image-id) expected_api_image_id="${2:?--expected-api-image-id requires a value}"; shift 2 ;;
    --expected-caddy-image-id) expected_caddy_image_id="${2:?--expected-caddy-image-id requires a value}"; shift 2 ;;
    --github-output) github_output="${2:?--github-output requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$artifact_dir" && -d "$artifact_dir" ]] || die "Release image artifact directory is unavailable."
[[ -n "$repository" ]] || die "Missing artifact repository identity."
[[ "$ci_run_id" =~ ^[0-9]+$ ]] || die "Missing or invalid CI workflow run ID."
[[ "$ci_run_attempt" =~ ^[1-9][0-9]*$ ]] || die "Missing or invalid CI workflow run attempt."
[[ -z "$expected_api_image_id" || "$expected_api_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid expected API image ID."
[[ -z "$expected_caddy_image_id" || "$expected_caddy_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid expected Caddy image ID."
[[ ( -z "$expected_api_image_id" && -z "$expected_caddy_image_id" ) || ( -n "$expected_api_image_id" && -n "$expected_caddy_image_id" ) ]] || \
  die "Expected API and Caddy image IDs must be provided together."
[[ -z "$github_output" || -e "$github_output" ]] || die "GitHub output file is not available."

archive_name="release-images-${release_commit}-${ci_run_id}-${ci_run_attempt}.tar.gz"
metadata_name="${archive_name%.tar.gz}.metadata"
archive="$artifact_dir/$archive_name"
checksum="$artifact_dir/$archive_name.sha256"
metadata="$artifact_dir/$metadata_name"
[[ -f "$archive" && -f "$checksum" && -f "$metadata" ]] || die "Verified release image artifact is incomplete."
mapfile -t checksum_lines < "$checksum"
[[ "${#checksum_lines[@]}" == "1" ]] || \
  die "Release image artifact checksum does not name the expected archive exactly once."
checksum_hash="${checksum_lines[0]%% *}"
[[ "$checksum_hash" =~ ^[0-9a-f]{64}$ && "${checksum_lines[0]}" == "$checksum_hash  $archive_name" ]] || \
  die "Release image artifact checksum does not name the expected archive exactly once."

(
  cd "$artifact_dir"
  sha256sum --check "$archive_name.sha256"
)
[[ "$(metadata_value "$metadata" repository)" == "$repository" ]] || die "Release image artifact repository does not match."
[[ "$(metadata_value "$metadata" release_sha)" == "$release_commit" ]] || die "Release image artifact release SHA does not match."
[[ "$(metadata_value "$metadata" ci_run_id)" == "$ci_run_id" ]] || die "Release image artifact CI run ID does not match."
[[ "$(metadata_value "$metadata" ci_run_attempt)" == "$ci_run_attempt" ]] || die "Release image artifact CI run attempt does not match."
[[ "$(metadata_value "$metadata" archive)" == "$archive_name" ]] || die "Release image artifact archive name does not match."

docker image load --input "$archive"

api_image="greatsellai-hr-api:$release_commit"
caddy_image="greatsellai-hr-caddy:$release_commit"
for image in "$api_image" "$caddy_image"; do
  [[ "$(image_label "$image" org.opencontainers.image.revision)" == "$release_commit" ]] || \
    die "Loaded image revision does not match the release commit: $image"
  [[ "$(image_label "$image" org.opencontainers.image.workflow_run_id)" == "$ci_run_id" ]] || \
    die "Loaded image CI workflow run ID does not match: $image"
  [[ "$(image_label "$image" org.opencontainers.image.workflow_run_attempt)" == "$ci_run_attempt" ]] || \
    die "Loaded image CI workflow run attempt does not match: $image"
done

api_image_id="$(docker image inspect --format '{{.Id}}' "$api_image")"
caddy_image_id="$(docker image inspect --format '{{.Id}}' "$caddy_image")"
[[ "$api_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Loaded API image ID is malformed."
[[ "$caddy_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Loaded Caddy image ID is malformed."
[[ -z "$expected_api_image_id" || "$api_image_id" == "$expected_api_image_id" ]] || \
  die "Loaded API image identity does not match completed staging."
[[ -z "$expected_caddy_image_id" || "$caddy_image_id" == "$expected_caddy_image_id" ]] || \
  die "Loaded Caddy image identity does not match completed staging."

if [[ -n "$github_output" ]]; then
  {
    printf 'api_image_id=%s\n' "$api_image_id"
    printf 'caddy_image_id=%s\n' "$caddy_image_id"
  } >> "$github_output"
fi

echo "CI-verified release images loaded for $release_commit."
