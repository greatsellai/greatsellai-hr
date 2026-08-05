#!/usr/bin/env bash
# Pull immutable TCR release images on a deployment host. The password crosses
# SSH only on standard input, never in an argument, environment file or log.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/pull-tcr-release-images.sh <commit-sha> [options] --password-stdin

Options:
  --host <ssh-host>                   Required deployment SSH target
  --registry <host>                   Required TCR registry hostname
  --username <name>                   Required TCR Docker username
  --api-registry-image <repo@digest>  Required immutable API TCR reference
  --caddy-registry-image <repo@digest>
                                       Required immutable Caddy TCR reference
  --api-image-config-digest <digest>  Required expected API config identity
  --caddy-image-config-digest <digest>
                                       Required expected Caddy config identity
  --expected-ci-run-id <id>           Required CI workflow run ID
  --expected-ci-run-attempt <number>  Required CI workflow run attempt
  --password-stdin                    Read exactly one TCR password line from stdin
  --ssh-key <path>                    Optional SSH private-key path; never committed

The remote host logs in with sudo Docker, pulls exact repo@sha256 references,
verifies config identity and OCI provenance labels, then assigns the existing
local commit tags consumed by Compose. No source, environment, database or
runtime service is changed.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

shell_quote() {
  printf '%q' "$1"
}

require_registry() {
  local registry="$1"
  [[ "$registry" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || \
    die "Invalid TCR registry hostname."
}

require_registry_image() {
  local image="$1" registry="$2" digest
  [[ "$image" == "$registry/"*@sha256:* ]] || die "Registry image does not belong to the configured TCR registry."
  digest="${image##*@}"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Registry image digest is malformed."
}

release_commit="${1:-}"
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || { usage >&2; exit 1; }
shift || true

remote_host=""
registry=""
username=""
api_registry_image=""
caddy_registry_image=""
api_image_config_digest=""
caddy_image_config_digest=""
expected_ci_run_id=""
expected_ci_run_attempt=""
password_stdin=0
ssh_key=""
while (($#)); do
  case "$1" in
    --host) remote_host="${2:?--host requires a value}"; shift 2 ;;
    --registry) registry="${2:?--registry requires a value}"; shift 2 ;;
    --username) username="${2:?--username requires a value}"; shift 2 ;;
    --api-registry-image) api_registry_image="${2:?--api-registry-image requires a value}"; shift 2 ;;
    --caddy-registry-image) caddy_registry_image="${2:?--caddy-registry-image requires a value}"; shift 2 ;;
    --api-image-config-digest) api_image_config_digest="${2:?--api-image-config-digest requires a value}"; shift 2 ;;
    --caddy-image-config-digest) caddy_image_config_digest="${2:?--caddy-image-config-digest requires a value}"; shift 2 ;;
    --expected-ci-run-id) expected_ci_run_id="${2:?--expected-ci-run-id requires a value}"; shift 2 ;;
    --expected-ci-run-attempt) expected_ci_run_attempt="${2:?--expected-ci-run-attempt requires a value}"; shift 2 ;;
    --password-stdin) password_stdin=1; shift ;;
    --ssh-key) ssh_key="${2:?--ssh-key requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$remote_host" ]] || die "Missing deployment target; pass --host."
require_registry "$registry"
[[ "$username" =~ ^[^[:space:]]+$ ]] || die "Missing or invalid TCR Docker username."
require_registry_image "$api_registry_image" "$registry"
require_registry_image "$caddy_registry_image" "$registry"
[[ "$api_registry_image" != "$caddy_registry_image" ]] || die "API and Caddy registry image references must differ."
[[ "$api_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid expected API config digest."
[[ "$caddy_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid expected Caddy config digest."
[[ "$expected_ci_run_id" =~ ^[0-9]+$ ]] || die "Invalid expected CI workflow run ID."
[[ "$expected_ci_run_attempt" =~ ^[1-9][0-9]*$ ]] || die "Invalid expected CI workflow run attempt."
[[ "$password_stdin" == "1" ]] || die "Refusing to read a TCR password except from standard input."
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."

if ! IFS= read -r registry_password; then
  die "Missing TCR password on standard input."
fi
[[ -n "$registry_password" ]] || die "TCR password on standard input is empty."
trap 'unset registry_password' EXIT

ssh_options=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o ConnectTimeout=20
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=10
)
if [[ -n "$ssh_key" ]]; then
  ssh_options+=(-i "$ssh_key" -o IdentitiesOnly=yes)
fi

remote_pull_script="$(cat <<'EOF'
set -Eeuo pipefail
release_commit="$1"
registry="$2"
username="$3"
api_registry_image="$4"
caddy_registry_image="$5"
api_image_config_digest="$6"
caddy_image_config_digest="$7"
expected_ci_run_id="$8"
expected_ci_run_attempt="$9"

die() { echo "TCR image pull error: $*" >&2; exit 1; }
image_label() { sudo -n docker image inspect --format "{{ index .Config.Labels \"$2\" }}" "$1"; }

[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || die "Invalid release commit."
[[ "$registry" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "Invalid registry hostname."
[[ "$username" =~ ^[^[:space:]]+$ ]] || die "Invalid TCR username."
[[ "$api_registry_image" == "$registry/"*@sha256:* ]] || die "API image is outside the configured registry."
[[ "$caddy_registry_image" == "$registry/"*@sha256:* ]] || die "Caddy image is outside the configured registry."
[[ "${api_registry_image##*@}" =~ ^sha256:[0-9a-f]{64}$ ]] || die "API registry digest is invalid."
[[ "${caddy_registry_image##*@}" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Caddy registry digest is invalid."
[[ "$api_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "API image config digest is invalid."
[[ "$caddy_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Caddy image config digest is invalid."
[[ "$expected_ci_run_id" =~ ^[0-9]+$ ]] || die "CI workflow run ID is invalid."
[[ "$expected_ci_run_attempt" =~ ^[1-9][0-9]*$ ]] || die "CI workflow run attempt is invalid."

if ! IFS= read -r registry_password; then
  die "Missing TCR password."
fi
[[ -n "$registry_password" ]] || die "TCR password is empty."
docker_config="$(mktemp -d "${TMPDIR:-/tmp}/greatsell-tcr.XXXXXXXX")" || die "Unable to create temporary Docker credential directory."
[[ "$docker_config" == "${TMPDIR:-/tmp}"/greatsell-tcr.* ]] || die "Refusing unexpected temporary Docker credential directory."
chmod 700 "$docker_config"
cleanup_docker_credentials() {
  sudo -n env DOCKER_CONFIG="$docker_config" docker logout "$registry" >/dev/null 2>&1 || true
  rm -rf -- "$docker_config"
}
trap cleanup_docker_credentials EXIT
printf '%s\n' "$registry_password" | sudo -n env DOCKER_CONFIG="$docker_config" docker login "$registry" --username "$username" --password-stdin >/dev/null
unset registry_password

platform="$(sudo -n docker version --format '{{.Server.Os}}/{{.Server.Arch}}')" || die "Unable to inspect Docker platform."
[[ "$platform" == "linux/amd64" ]] || die "Deployment target platform must be linux/amd64; got $platform."

require_exact_image() {
  local registry_image="$1" local_image="$2" expected_config="$3"
  local observed_config revision image_run_id image_run_attempt repo_digest found=0
  sudo -n env DOCKER_CONFIG="$docker_config" docker pull "$registry_image" >/dev/null
  observed_config="$(sudo -n docker image inspect --format '{{.Id}}' "$registry_image")"
  [[ "$observed_config" == "$expected_config" ]] || die "Pulled image config identity does not match CI metadata: $registry_image"
  while IFS= read -r repo_digest; do
    [[ "$repo_digest" == "$registry_image" ]] && found=1
  done < <(sudo -n docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$registry_image")
  [[ "$found" == "1" ]] || die "Pulled image is not associated with its exact TCR manifest digest: $registry_image"
  revision="$(image_label "$registry_image" org.opencontainers.image.revision)"
  image_run_id="$(image_label "$registry_image" org.opencontainers.image.workflow_run_id)"
  image_run_attempt="$(image_label "$registry_image" org.opencontainers.image.workflow_run_attempt)"
  [[ "$revision" == "$release_commit" ]] || die "Pulled image revision does not match the release commit: $registry_image"
  [[ "$image_run_id" == "$expected_ci_run_id" ]] || die "Pulled image CI run ID does not match: $registry_image"
  [[ "$image_run_attempt" == "$expected_ci_run_attempt" ]] || die "Pulled image CI run attempt does not match: $registry_image"
  sudo -n docker tag "$registry_image" "$local_image"
  [[ "$(sudo -n docker image inspect --format '{{.Id}}' "$local_image")" == "$expected_config" ]] || \
    die "Local Compose image tag was not assigned to the verified TCR image: $local_image"
}

require_exact_image "$api_registry_image" "greatsellai-hr-api:$release_commit" "$api_image_config_digest"
require_exact_image "$caddy_registry_image" "greatsellai-hr-caddy:$release_commit" "$caddy_image_config_digest"
echo "TCR images pulled and verified for $release_commit."
EOF
)"

remote_command="bash -c $(shell_quote "$remote_pull_script") -- $(shell_quote "$release_commit") $(shell_quote "$registry") $(shell_quote "$username") $(shell_quote "$api_registry_image") $(shell_quote "$caddy_registry_image") $(shell_quote "$api_image_config_digest") $(shell_quote "$caddy_image_config_digest") $(shell_quote "$expected_ci_run_id") $(shell_quote "$expected_ci_run_attempt")"
printf '%s\n' "$registry_password" | ssh "${ssh_options[@]}" "$remote_host" "$remote_command"
unset registry_password

echo "TCR release images are available on $remote_host for $release_commit."
