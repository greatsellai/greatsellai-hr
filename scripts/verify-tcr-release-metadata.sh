#!/usr/bin/env bash
# Verify the small CI metadata artifact that binds a merged main commit to the
# exact TCR manifest digests the deployment environments must pull.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/verify-tcr-release-metadata.sh <commit-sha> [options]

Options:
  --artifact-dir <path>             Required downloaded CI metadata directory
  --repository <owner/repository>   Required repository recorded in metadata
  --ci-run-id <id>                  Required CI workflow run ID
  --ci-run-attempt <number>         Required CI workflow run attempt
  --registry <host>                 Required TCR registry hostname
  --namespace <name>                Required TCR namespace
  --github-output <path>            Optional GitHub output file

The artifact contains no credentials. Its checksum, run identity, image
config identities and digest-pinned registry references are all verified before
any staging or production machine is allowed to pull an image.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

metadata_value() {
  local metadata="$1" key="$2" value count
  count="$(grep -c "^${key}=" "$metadata" || true)"
  [[ "$count" == "1" ]] || die "TCR release metadata is missing or repeats '$key'."
  value="$(sed -n "s/^${key}=//p" "$metadata")"
  [[ -n "$value" ]] || die "TCR release metadata has an empty '$key'."
  printf '%s' "$value"
}

require_registry() {
  local registry="$1"
  [[ "$registry" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || \
    die "Invalid TCR registry hostname."
}

require_namespace() {
  local namespace="$1"
  [[ "$namespace" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] || die "Invalid TCR namespace."
}

verify_registry_image() {
  local image="$1" repository="$2" digest
  [[ "$image" == "$repository@sha256:"* ]] || die "TCR registry image does not match its expected repository."
  digest="${image#"$repository"@}"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "TCR registry image digest is malformed."
}

release_commit="${1:-}"
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || { usage >&2; exit 1; }
shift || true

artifact_dir=""
repository=""
ci_run_id=""
ci_run_attempt=""
registry=""
namespace=""
github_output=""
while (($#)); do
  case "$1" in
    --artifact-dir) artifact_dir="${2:?--artifact-dir requires a value}"; shift 2 ;;
    --repository) repository="${2:?--repository requires a value}"; shift 2 ;;
    --ci-run-id) ci_run_id="${2:?--ci-run-id requires a value}"; shift 2 ;;
    --ci-run-attempt) ci_run_attempt="${2:?--ci-run-attempt requires a value}"; shift 2 ;;
    --registry) registry="${2:?--registry requires a value}"; shift 2 ;;
    --namespace) namespace="${2:?--namespace requires a value}"; shift 2 ;;
    --github-output) github_output="${2:?--github-output requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$artifact_dir" && -d "$artifact_dir" ]] || die "Release metadata artifact directory is unavailable."
[[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "Invalid repository identity."
[[ "$ci_run_id" =~ ^[0-9]+$ ]] || die "Missing or invalid CI workflow run ID."
[[ "$ci_run_attempt" =~ ^[1-9][0-9]*$ ]] || die "Missing or invalid CI workflow run attempt."
require_registry "$registry"
require_namespace "$namespace"
[[ -z "$github_output" || -e "$github_output" ]] || die "GitHub output file is not available."

metadata_name="release-image-metadata-${release_commit}-${ci_run_id}-${ci_run_attempt}.env"
checksum_name="${metadata_name}.sha256"
metadata="$artifact_dir/$metadata_name"
checksum="$artifact_dir/$checksum_name"
[[ -f "$metadata" && -f "$checksum" ]] || die "TCR release metadata artifact is incomplete."
mapfile -t checksum_lines < "$checksum"
[[ "${#checksum_lines[@]}" == "1" ]] || die "TCR metadata checksum must name the metadata file exactly once."
metadata_sha256="${checksum_lines[0]%% *}"
[[ "$metadata_sha256" =~ ^[0-9a-f]{64}$ && "${checksum_lines[0]}" == "$metadata_sha256  $metadata_name" ]] || \
  die "TCR metadata checksum must name the metadata file exactly once."
(
  cd "$artifact_dir"
  sha256sum --check "$checksum_name"
)

[[ "$(metadata_value "$metadata" format_version)" == "1" ]] || die "Unsupported TCR release metadata format."
[[ "$(metadata_value "$metadata" repository)" == "$repository" ]] || die "TCR metadata repository does not match."
[[ "$(metadata_value "$metadata" release_sha)" == "$release_commit" ]] || die "TCR metadata release SHA does not match."
[[ "$(metadata_value "$metadata" ci_run_id)" == "$ci_run_id" ]] || die "TCR metadata CI run ID does not match."
[[ "$(metadata_value "$metadata" ci_run_attempt)" == "$ci_run_attempt" ]] || die "TCR metadata CI run attempt does not match."

api_registry_image="$(metadata_value "$metadata" api_registry_image)"
caddy_registry_image="$(metadata_value "$metadata" caddy_registry_image)"
api_image_config_digest="$(metadata_value "$metadata" api_image_config_digest)"
caddy_image_config_digest="$(metadata_value "$metadata" caddy_image_config_digest)"
verify_registry_image "$api_registry_image" "$registry/$namespace/hr-api"
verify_registry_image "$caddy_registry_image" "$registry/$namespace/hr-caddy"
[[ "$api_registry_image" != "$caddy_registry_image" ]] || die "TCR API and Caddy image references must differ."
[[ "$api_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "TCR API config digest is malformed."
[[ "$caddy_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "TCR Caddy config digest is malformed."

if [[ -n "$github_output" ]]; then
  {
    printf 'image_metadata_sha256=%s\n' "$metadata_sha256"
    printf 'api_registry_image=%s\n' "$api_registry_image"
    printf 'caddy_registry_image=%s\n' "$caddy_registry_image"
    printf 'api_image_config_digest=%s\n' "$api_image_config_digest"
    printf 'caddy_image_config_digest=%s\n' "$caddy_image_config_digest"
  } >> "$github_output"
fi

echo "Verified immutable TCR release metadata for $release_commit."
