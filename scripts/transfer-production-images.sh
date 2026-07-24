#!/usr/bin/env bash
# Transfers the exact CI-built production images to the production host without
# rebuilding application layers on that host.  It intentionally uses the same
# immutable commit tag Compose already consumes in production.
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Usage: scripts/transfer-production-images.sh <commit-sha> --host <ssh-host> [options]

Options:
  --ssh-key <path>     Optional SSH private-key path; never committed

The source runner must already contain the CI-verified API and Caddy images
tagged with the full commit SHA. The remote host receives the image stream only;
no production environment file, database, upload, or source file is changed.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

shell_quote() {
  printf '%q' "$1"
}

image_revision() {
  docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$1"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

release_commit="${1:-}"
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || {
  usage >&2
  die "Expected a full 40-character lowercase commit SHA."
}
shift || true

remote_host=""
ssh_key=""
while (($#)); do
  case "$1" in
    --host) remote_host="${2:?--host requires a value}"; shift 2 ;;
    --ssh-key) ssh_key="${2:?--ssh-key requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$remote_host" ]] || die "Missing deployment target; pass --host."
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."

api_image="greatsellai-hr-api:$release_commit"
caddy_image="greatsellai-hr-caddy:$release_commit"
for image in "$api_image" "$caddy_image"; do
  revision="$(image_revision "$image")" || die "Required CI image is unavailable: $image"
  [[ "$revision" == "$release_commit" ]] || \
    die "CI image $image does not carry the expected immutable revision label."
done

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

remote_loader="$(cat <<'EOF'
set -Eeuo pipefail
release_commit="$1"
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Invalid release commit." >&2
  exit 1
}

api_image="greatsellai-hr-api:$release_commit"
caddy_image="greatsellai-hr-caddy:$release_commit"

gzip -dc | sudo -n docker image load
for image in "$api_image" "$caddy_image"; do
  revision="$(sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")" || {
    echo "Transferred image is unavailable: $image" >&2
    exit 1
  }
  [[ "$revision" == "$release_commit" ]] || {
    echo "Transferred image revision label does not match the release commit: $image" >&2
    exit 1
  }
done
EOF
)"
remote_command="bash -c $(shell_quote "$remote_loader") -- $(shell_quote "$release_commit")"

echo "Streaming CI-verified production images for $release_commit to $remote_host."
docker image save "$api_image" "$caddy_image" | gzip -1 | \
  ssh "${ssh_options[@]}" "$remote_host" "$remote_command"
echo "Production images transferred and verified for $release_commit."
