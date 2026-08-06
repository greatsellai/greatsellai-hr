#!/usr/bin/env bash
# Verify a completed staging attestation without modifying either environment.
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Usage: scripts/verify-staging-release.sh <stg-tag> <commit-sha> <archive-sha256> [options]

Options:
  --host <ssh-host>         Required staging SSH target
  --project-dir <path>      Required isolated staging project directory
  --history-dir <path>      Required staging history directory
  --ssh-key <path>          Optional SSH private-key path; never committed
  --github-output <path>    Write verified non-secret identifiers for a workflow job

The command only accepts a completed public-smoke-tested staging record with
the exact tag, source checksum, CI-attested TCR manifest references, image
config identities, and image revisions.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

shell_quote() {
  printf '%q' "$1"
}

tag="${1:-}"
release_commit="${2:-}"
archive_sha256="${3:-}"
[[ -n "$tag" && -n "$release_commit" && -n "$archive_sha256" ]] || { usage >&2; exit 1; }
shift 3

remote_host=""
project_dir=""
history_dir=""
ssh_key=""
github_output=""
while (($#)); do
  case "$1" in
    --host) remote_host="${2:?--host requires a value}"; shift 2 ;;
    --project-dir) project_dir="${2:?--project-dir requires a value}"; shift 2 ;;
    --history-dir) history_dir="${2:?--history-dir requires a value}"; shift 2 ;;
    --ssh-key) ssh_key="${2:?--ssh-key requires a value}"; shift 2 ;;
    --github-output) github_output="${2:?--github-output requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ "$tag" =~ ^stg-[0-9]{8}-[0-9a-f]{7,40}$ ]] || die "Invalid staging tag."
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || die "Invalid release commit."
[[ "$archive_sha256" =~ ^[0-9a-f]{64}$ ]] || die "Invalid source archive checksum."
[[ -n "$remote_host" && -n "$project_dir" && -n "$history_dir" ]] || die "Missing staging target configuration."
[[ "$project_dir" == /home/ubuntu/* && "$project_dir" == *staging* && "$project_dir" != /home/ubuntu/ ]] || die "Unsafe staging project directory."
[[ "$history_dir" == /home/ubuntu/* && "$history_dir" == *staging* && "$history_dir" != /home/ubuntu/ ]] || die "Unsafe staging history directory."
[[ "$project_dir" != "$history_dir" ]] || die "Staging project and history directories must be distinct."
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."
[[ -z "$github_output" || -e "$github_output" ]] || die "GitHub output file is not available."

git fetch origin main --tags --prune
tag_commit="$(git rev-parse -q --verify "refs/tags/$tag^{commit}")" || die "Unknown staging tag."
[[ "$tag_commit" == "$release_commit" ]] || die "Staging tag does not point to the requested commit."
[[ "$release_commit" == "$(git rev-parse origin/main)" ]] || die "Staging candidate is no longer current origin/main."

ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=yes)
if [[ -n "$ssh_key" ]]; then
  ssh_options+=(-i "$ssh_key" -o IdentitiesOnly=yes)
fi

remote_verify_script="$(cat <<'EOF'
set -Eeuo pipefail
project_dir="$1"
history_dir="$2"
tag="$3"
release_commit="$4"
archive_sha256="$5"
record="$history_dir/current-release.env"

die() { echo "Staging verification error: $*" >&2; exit 1; }
record_value() { sed -n "s/^$2=//p" "$1" | tail -n 1; }
image_id() { sudo -n docker image inspect --format '{{.Id}}' "$1"; }
image_revision() { sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$1"; }
image_ci_run_id() { sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.workflow_run_id" }}' "$1"; }
image_ci_run_attempt() { sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.workflow_run_attempt" }}' "$1"; }
has_registry_image() {
  local image="$1" expected="$2" observed
  while IFS= read -r observed; do
    [[ "$observed" == "$expected" ]] && return 0
  done < <(sudo -n docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image")
  return 1
}

[[ "$project_dir" == /home/ubuntu/* && "$project_dir" == *staging* && "$project_dir" != /home/ubuntu/ ]] || die "Unsafe staging project directory."
[[ "$history_dir" == /home/ubuntu/* && "$history_dir" == *staging* && "$history_dir" != /home/ubuntu/ ]] || die "Unsafe staging history directory."
[[ "$project_dir" != "$history_dir" ]] || die "Staging project and history directories must be distinct."
test -f "$project_dir/.env.staging"
test ! -e "$project_dir/.env.production"
test -f "$project_dir/compose.yml"
test -f "$record"
[[ "$(record_value "$record" state)" == "complete" ]] || die "Staging public smoke check has not completed."
[[ "$(record_value "$record" tag)" == "$tag" ]] || die "Staging record tag does not match."
[[ "$(record_value "$record" commit)" == "$release_commit" ]] || die "Staging record commit does not match."
[[ "$(record_value "$record" archive_sha256)" == "$archive_sha256" ]] || die "Staging source checksum does not match."
[[ "$(record_value "$record" private_api_health_check)" == "pass" ]] || die "Staging private API health was not recorded as passing."
[[ "$(record_value "$record" private_edge_health_check)" == "pass" ]] || die "Staging private edge health was not recorded as passing."
[[ "$(record_value "$record" public_smoke_check)" == "pass" ]] || die "Staging public smoke check was not recorded as passing."
image_metadata_sha256="$(record_value "$record" image_metadata_sha256)"
api_registry_image="$(record_value "$record" api_registry_image)"
caddy_registry_image="$(record_value "$record" caddy_registry_image)"
api_image_config_digest="$(record_value "$record" api_image_config_digest)"
caddy_image_config_digest="$(record_value "$record" caddy_image_config_digest)"
[[ "$image_metadata_sha256" =~ ^[0-9a-f]{64}$ ]] || die "Staging CI image metadata checksum is invalid."
[[ "$api_registry_image" =~ ^[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}$ ]] || die "Staging API TCR registry image is invalid."
[[ "$caddy_registry_image" =~ ^[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}$ ]] || die "Staging Caddy TCR registry image is invalid."
[[ "$api_registry_image" != "$caddy_registry_image" ]] || die "Staging TCR registry images must differ."
[[ "$api_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Staging API image config digest is invalid."
[[ "$caddy_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Staging Caddy image config digest is invalid."

api_image="greatsellai-hr-api:$release_commit"
caddy_image="greatsellai-hr-caddy:$release_commit"
api_image_id="$(image_id "$api_image")" || die "Staging API image is unavailable."
caddy_image_id="$(image_id "$caddy_image")" || die "Staging Caddy image is unavailable."
[[ "$api_image_id" == "$(record_value "$record" api_image_id)" ]] || die "Staging API image identity no longer matches its attestation."
[[ "$caddy_image_id" == "$(record_value "$record" caddy_image_id)" ]] || die "Staging Caddy image identity no longer matches its attestation."
[[ "$api_image_id" == "$api_image_config_digest" ]] || die "Staging API image config identity differs from CI TCR metadata."
[[ "$caddy_image_id" == "$caddy_image_config_digest" ]] || die "Staging Caddy image config identity differs from CI TCR metadata."
has_registry_image "$api_image" "$api_registry_image" || die "Staging API image is not associated with its exact TCR manifest."
has_registry_image "$caddy_image" "$caddy_registry_image" || die "Staging Caddy image is not associated with its exact TCR manifest."
[[ "$(image_revision "$api_image")" == "$release_commit" ]] || die "Staging API image revision is invalid."
[[ "$(image_revision "$caddy_image")" == "$release_commit" ]] || die "Staging Caddy image revision is invalid."
api_ci_run_id="$(image_ci_run_id "$api_image")"
caddy_ci_run_id="$(image_ci_run_id "$caddy_image")"
api_ci_run_attempt="$(image_ci_run_attempt "$api_image")"
caddy_ci_run_attempt="$(image_ci_run_attempt "$caddy_image")"
[[ "$api_ci_run_id" =~ ^[0-9]+$ && "$api_ci_run_id" == "$caddy_ci_run_id" ]] || die "Staging image CI workflow run identities are invalid."
[[ "$api_ci_run_attempt" =~ ^[1-9][0-9]*$ && "$api_ci_run_attempt" == "$caddy_ci_run_attempt" ]] || die "Staging image CI workflow run attempts are invalid."

api_container="$(sudo -n env "RESUME_V3_RELEASE_IMAGE_TAG=$release_commit" docker compose --project-directory "$project_dir" -f "$project_dir/compose.yml" --env-file "$project_dir/.env.staging" ps -q api)"
caddy_container="$(sudo -n env "RESUME_V3_RELEASE_IMAGE_TAG=$release_commit" docker compose --project-directory "$project_dir" -f "$project_dir/compose.yml" --env-file "$project_dir/.env.staging" ps -q caddy)"
[[ -n "$api_container" && -n "$caddy_container" ]] || die "Staging runtime containers are missing."
[[ "$(sudo -n docker inspect --format '{{.Image}}' "$api_container")" == "$api_image_id" ]] || die "Staging API container differs from attested image."
[[ "$(sudo -n docker inspect --format '{{.Image}}' "$caddy_container")" == "$caddy_image_id" ]] || die "Staging Caddy container differs from attested image."

printf 'image_metadata_sha256=%s\n' "$image_metadata_sha256"
printf 'api_registry_image=%s\n' "$api_registry_image"
printf 'caddy_registry_image=%s\n' "$caddy_registry_image"
printf 'api_image_config_digest=%s\n' "$api_image_config_digest"
printf 'caddy_image_config_digest=%s\n' "$caddy_image_config_digest"
printf 'ci_run_id=%s\n' "$api_ci_run_id"
printf 'ci_run_attempt=%s\n' "$api_ci_run_attempt"
EOF
)"

verification="$(ssh "${ssh_options[@]}" "$remote_host" \
  "bash -c $(shell_quote "$remote_verify_script") -- $(shell_quote "$project_dir") $(shell_quote "$history_dir") $(shell_quote "$tag") $(shell_quote "$release_commit") $(shell_quote "$archive_sha256")")"
image_metadata_sha256="$(printf '%s\n' "$verification" | sed -n 's/^image_metadata_sha256=//p' | tail -n 1)"
api_registry_image="$(printf '%s\n' "$verification" | sed -n 's/^api_registry_image=//p' | tail -n 1)"
caddy_registry_image="$(printf '%s\n' "$verification" | sed -n 's/^caddy_registry_image=//p' | tail -n 1)"
api_image_config_digest="$(printf '%s\n' "$verification" | sed -n 's/^api_image_config_digest=//p' | tail -n 1)"
caddy_image_config_digest="$(printf '%s\n' "$verification" | sed -n 's/^caddy_image_config_digest=//p' | tail -n 1)"
ci_run_id="$(printf '%s\n' "$verification" | sed -n 's/^ci_run_id=//p' | tail -n 1)"
ci_run_attempt="$(printf '%s\n' "$verification" | sed -n 's/^ci_run_attempt=//p' | tail -n 1)"
[[ "$image_metadata_sha256" =~ ^[0-9a-f]{64}$ ]] || die "Staging CI image metadata checksum is malformed."
[[ "$api_registry_image" =~ ^[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}$ ]] || die "Staging API TCR registry image is malformed."
[[ "$caddy_registry_image" =~ ^[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}$ ]] || die "Staging Caddy TCR registry image is malformed."
[[ "$api_registry_image" != "$caddy_registry_image" ]] || die "Staging TCR registry images must differ."
[[ "$api_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Staging API image config digest is malformed."
[[ "$caddy_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Staging Caddy image config digest is malformed."
[[ "$ci_run_id" =~ ^[0-9]+$ ]] || die "Staging CI workflow run ID is malformed."
[[ "$ci_run_attempt" =~ ^[1-9][0-9]*$ ]] || die "Staging CI workflow run attempt is malformed."

if [[ -n "$github_output" ]]; then
  {
    printf 'staging_tag=%s\n' "$tag"
    printf 'release_sha=%s\n' "$release_commit"
    printf 'archive_sha256=%s\n' "$archive_sha256"
    printf 'image_metadata_sha256=%s\n' "$image_metadata_sha256"
    printf 'api_registry_image=%s\n' "$api_registry_image"
    printf 'caddy_registry_image=%s\n' "$caddy_registry_image"
    printf 'api_image_config_digest=%s\n' "$api_image_config_digest"
    printf 'caddy_image_config_digest=%s\n' "$caddy_image_config_digest"
    printf 'ci_run_id=%s\n' "$ci_run_id"
    printf 'ci_run_attempt=%s\n' "$ci_run_attempt"
  } >> "$github_output"
fi

echo "Verified completed staging attestation for $tag ($release_commit)."
