#!/usr/bin/env bash
# Verify the exact staged OCI image identities on a promotion target.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/verify-release-images.sh <commit-sha> [options]

Options:
  --host <ssh-host>         Required production SSH target
  --api-image-id <sha256>   Required image ID recorded by staging
  --caddy-image-id <sha256> Required image ID recorded by staging
  --ssh-key <path>          Optional SSH private-key path; never committed

This command does not transfer, build, tag, or deploy images. It fails closed
unless the promotion target already holds the exact staging-attested images.
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
api_image_id=""
caddy_image_id=""
ssh_key=""
while (($#)); do
  case "$1" in
    --host) remote_host="${2:?--host requires a value}"; shift 2 ;;
    --api-image-id) api_image_id="${2:?--api-image-id requires a value}"; shift 2 ;;
    --caddy-image-id) caddy_image_id="${2:?--caddy-image-id requires a value}"; shift 2 ;;
    --ssh-key) ssh_key="${2:?--ssh-key requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$remote_host" ]] || die "Missing promotion target; pass --host."
[[ "$api_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid staged API image ID."
[[ "$caddy_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid staged Caddy image ID."
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."

ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=yes)
if [[ -n "$ssh_key" ]]; then
  ssh_options+=(-i "$ssh_key" -o IdentitiesOnly=yes)
fi

remote_verify_script="$(cat <<'EOF'
set -Eeuo pipefail
release_commit="$1"
api_expected="$2"
caddy_expected="$3"

platform="$(sudo -n docker version --format '{{.Server.Os}}/{{.Server.Arch}}')" || {
  echo "Unable to inspect promotion Docker platform." >&2
  exit 1
}
[[ "$platform" == "linux/amd64" ]] || {
  echo "Promotion target platform must be linux/amd64; got $platform." >&2
  exit 1
}

require_image() {
  local image="$1" expected_id="$2" observed_id revision
  observed_id="$(sudo -n docker image inspect --format '{{.Id}}' "$image")" || {
    echo "Promotion image is unavailable: $image" >&2
    exit 1
  }
  revision="$(sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")"
  [[ "$observed_id" == "$expected_id" ]] || {
    echo "Promotion image identity does not match completed staging: $image" >&2
    exit 1
  }
  [[ "$revision" == "$release_commit" ]] || {
    echo "Promotion image revision does not match completed staging: $image" >&2
    exit 1
  }
}

require_image "greatsellai-hr-api:$release_commit" "$api_expected"
require_image "greatsellai-hr-caddy:$release_commit" "$caddy_expected"
EOF
)"

ssh "${ssh_options[@]}" "$remote_host" \
  "bash -c $(shell_quote "$remote_verify_script") -- $(shell_quote "$release_commit") $(shell_quote "$api_image_id") $(shell_quote "$caddy_image_id")"

echo "Promotion target holds the exact completed-staging images for $release_commit."
