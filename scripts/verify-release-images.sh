#!/usr/bin/env bash
# Verify CI-attested images on a promotion target without comparing
# daemon-local image IDs across different Docker implementations.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/verify-release-images.sh <commit-sha> [options]

Options:
  --host <ssh-host>         Required production SSH target
  --api-registry-image <repo@digest>
                            Required exact API TCR manifest reference
  --caddy-registry-image <repo@digest>
                            Required exact Caddy TCR manifest reference
  --api-image-config-digest <sha256>
                            Required expected API config identity
  --caddy-image-config-digest <sha256>
                            Required expected Caddy config identity
  --expected-ci-run-id <id> Required CI workflow run ID attested by staging
  --expected-ci-run-attempt <number>
                            Required CI workflow run attempt attested by staging
  --ssh-key <path>          Optional SSH private-key path; never committed

This command does not transfer, build, tag, or deploy images. It fails closed
unless the promotion target holds the completed staging candidate's exact TCR
manifest references, config identities, immutable revision and CI provenance.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

shell_quote() {
  printf '%q' "$1"
}

release_commit="${1:-}"
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || { usage >&2; exit 1; }
shift

remote_host=""
api_registry_image=""
caddy_registry_image=""
api_image_config_digest=""
caddy_image_config_digest=""
expected_ci_run_id=""
expected_ci_run_attempt=""
ssh_key=""
while (($#)); do
  case "$1" in
    --host) remote_host="${2:?--host requires a value}"; shift 2 ;;
    --api-registry-image) api_registry_image="${2:?--api-registry-image requires a value}"; shift 2 ;;
    --caddy-registry-image) caddy_registry_image="${2:?--caddy-registry-image requires a value}"; shift 2 ;;
    --api-image-config-digest) api_image_config_digest="${2:?--api-image-config-digest requires a value}"; shift 2 ;;
    --caddy-image-config-digest) caddy_image_config_digest="${2:?--caddy-image-config-digest requires a value}"; shift 2 ;;
    --expected-ci-run-id) expected_ci_run_id="${2:?--expected-ci-run-id requires a value}"; shift 2 ;;
    --expected-ci-run-attempt) expected_ci_run_attempt="${2:?--expected-ci-run-attempt requires a value}"; shift 2 ;;
    --ssh-key) ssh_key="${2:?--ssh-key requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$remote_host" ]] || die "Missing promotion target; pass --host."
[[ "$api_registry_image" =~ ^[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}$ ]] || die "Invalid expected API TCR registry image."
[[ "$caddy_registry_image" =~ ^[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}$ ]] || die "Invalid expected Caddy TCR registry image."
[[ "$api_registry_image" != "$caddy_registry_image" ]] || die "Expected API and Caddy TCR registry images must differ."
[[ "$api_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid expected API config digest."
[[ "$caddy_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid expected Caddy config digest."
[[ "$expected_ci_run_id" =~ ^[0-9]+$ ]] || die "Invalid expected CI workflow run ID."
[[ "$expected_ci_run_attempt" =~ ^[1-9][0-9]*$ ]] || die "Invalid expected CI workflow run attempt."
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."

ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=yes)
if [[ -n "$ssh_key" ]]; then
  ssh_options+=(-i "$ssh_key" -o IdentitiesOnly=yes)
fi

remote_verify_script="$(cat <<'EOF'
set -Eeuo pipefail
release_commit="$1"
expected_ci_run_id="$2"
expected_ci_run_attempt="$3"
api_registry_image="$4"
caddy_registry_image="$5"
api_image_config_digest="$6"
caddy_image_config_digest="$7"

[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Invalid promotion release commit." >&2
  exit 1
}
[[ "$expected_ci_run_id" =~ ^[0-9]+$ ]] || {
  echo "Invalid expected CI workflow run ID." >&2
  exit 1
}
[[ "$expected_ci_run_attempt" =~ ^[1-9][0-9]*$ ]] || {
  echo "Invalid expected CI workflow run attempt." >&2
  exit 1
}
[[ "$api_registry_image" =~ ^[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}$ ]] || {
  echo "Invalid expected API TCR registry image." >&2
  exit 1
}
[[ "$caddy_registry_image" =~ ^[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}$ ]] || {
  echo "Invalid expected Caddy TCR registry image." >&2
  exit 1
}
[[ "$api_registry_image" != "$caddy_registry_image" ]] || {
  echo "Expected API and Caddy TCR registry images must differ." >&2
  exit 1
}
[[ "$api_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "Invalid expected API config digest." >&2
  exit 1
}
[[ "$caddy_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "Invalid expected Caddy config digest." >&2
  exit 1
}

platform="$(sudo -n docker version --format '{{.Server.Os}}/{{.Server.Arch}}')" || {
  echo "Unable to inspect promotion Docker platform." >&2
  exit 1
}
[[ "$platform" == "linux/amd64" ]] || {
  echo "Promotion target platform must be linux/amd64; got $platform." >&2
  exit 1
}

require_image() {
  local image="$1" expected_registry_image="$2" expected_config_digest="$3"
  local observed_id revision ci_run_id ci_run_attempt repo_digest found=0
  observed_id="$(sudo -n docker image inspect --format '{{.Id}}' "$image")" || {
    echo "Promotion image is unavailable: $image" >&2
    exit 1
  }
  revision="$(sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")"
  [[ "$observed_id" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "Promotion image has an invalid local image ID: $image" >&2
    exit 1
  }
  [[ "$observed_id" == "$expected_config_digest" ]] || {
    echo "Promotion image config identity does not match completed staging: $image" >&2
    exit 1
  }
  while IFS= read -r repo_digest; do
    [[ "$repo_digest" == "$expected_registry_image" ]] && found=1
  done < <(sudo -n docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image")
  [[ "$found" == "1" ]] || {
    echo "Promotion image is not associated with the completed staging TCR manifest: $image" >&2
    exit 1
  }
  [[ "$revision" == "$release_commit" ]] || {
    echo "Promotion image revision does not match completed staging: $image" >&2
    exit 1
  }
  ci_run_id="$(sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.workflow_run_id" }}' "$image")"
  [[ "$ci_run_id" == "$expected_ci_run_id" ]] || {
    echo "Promotion image CI workflow run ID does not match completed staging: $image" >&2
    exit 1
  }
  ci_run_attempt="$(sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.workflow_run_attempt" }}' "$image")"
  [[ "$ci_run_attempt" == "$expected_ci_run_attempt" ]] || {
    echo "Promotion image CI workflow run attempt does not match completed staging: $image" >&2
    exit 1
  }
}

require_image "greatsellai-hr-api:$release_commit" "$api_registry_image" "$api_image_config_digest"
require_image "greatsellai-hr-caddy:$release_commit" "$caddy_registry_image" "$caddy_image_config_digest"
EOF
)"

ssh "${ssh_options[@]}" "$remote_host" \
  "bash -c $(shell_quote "$remote_verify_script") -- $(shell_quote "$release_commit") $(shell_quote "$expected_ci_run_id") $(shell_quote "$expected_ci_run_attempt") $(shell_quote "$api_registry_image") $(shell_quote "$caddy_registry_image") $(shell_quote "$api_image_config_digest") $(shell_quote "$caddy_image_config_digest")"

echo "Promotion target holds CI-attested images for $release_commit."
