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
  --archive <path>         Required verified CI release-image .tar.gz archive
  --archive-sha256 <hash>  Required SHA-256 of the exact archive bytes
  --expected-ci-run-id <id>
                       Required successful CI workflow run ID recorded on both images
  --expected-ci-run-attempt <number>
                       Required successful CI workflow run attempt recorded on both images
  --ssh-key <path>     Optional SSH private-key path; never committed

The source runner must already have loaded the CI-verified API and Caddy images
tagged with the full commit SHA. The remote host receives the exact verified CI
archive bytes, verifies their SHA-256 before loading, and never receives a
daemon-specific re-export. No environment file, database, upload, or source
file is changed.
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

image_ci_run_id() {
  docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.workflow_run_id" }}' "$1"
}

image_ci_run_attempt() {
  docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.workflow_run_attempt" }}' "$1"
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
archive=""
archive_sha256=""
expected_ci_run_id=""
expected_ci_run_attempt=""
ssh_key=""
while (($#)); do
  case "$1" in
    --host) remote_host="${2:?--host requires a value}"; shift 2 ;;
    --archive) archive="${2:?--archive requires a value}"; shift 2 ;;
    --archive-sha256) archive_sha256="${2:?--archive-sha256 requires a value}"; shift 2 ;;
    --expected-ci-run-id) expected_ci_run_id="${2:?--expected-ci-run-id requires a value}"; shift 2 ;;
    --expected-ci-run-attempt) expected_ci_run_attempt="${2:?--expected-ci-run-attempt requires a value}"; shift 2 ;;
    --ssh-key) ssh_key="${2:?--ssh-key requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$remote_host" ]] || die "Missing deployment target; pass --host."
[[ -n "$archive" && -f "$archive" && -r "$archive" ]] || die "Missing readable verified CI release-image archive."
[[ "$archive_sha256" =~ ^[0-9a-f]{64}$ ]] || die "Missing or invalid CI release-image archive checksum."
[[ "$expected_ci_run_id" =~ ^[0-9]+$ ]] || die "Missing or invalid CI workflow run ID."
[[ "$expected_ci_run_attempt" =~ ^[1-9][0-9]*$ ]] || die "Missing or invalid CI workflow run attempt."
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."
[[ "$(sha256sum "$archive" | awk '{print $1}')" == "$archive_sha256" ]] || \
  die "Verified CI release-image archive checksum changed before transfer."

api_image="greatsellai-hr-api:$release_commit"
caddy_image="greatsellai-hr-caddy:$release_commit"
for image in "$api_image" "$caddy_image"; do
  revision="$(image_revision "$image")" || die "Required CI image is unavailable: $image"
  image_run_id="$(image_ci_run_id "$image")" || die "Required CI provenance label is unavailable: $image"
  image_run_attempt="$(image_ci_run_attempt "$image")" || die "Required CI attempt provenance label is unavailable: $image"
  [[ "$revision" == "$release_commit" ]] || \
    die "CI image $image does not carry the expected immutable revision label."
  [[ "$image_run_id" == "$expected_ci_run_id" ]] || \
    die "CI image $image does not come from the expected successful workflow run."
  [[ "$image_run_attempt" == "$expected_ci_run_attempt" ]] || \
    die "CI image $image does not come from the expected successful workflow run attempt."
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
expected_ci_run_id="$2"
expected_ci_run_attempt="$3"
expected_archive_sha256="$4"
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Invalid release commit." >&2
  exit 1
}
[[ "$expected_ci_run_id" =~ ^[0-9]+$ ]] || {
  echo "Invalid CI workflow run ID." >&2
  exit 1
}
[[ "$expected_ci_run_attempt" =~ ^[1-9][0-9]*$ ]] || {
  echo "Invalid CI workflow run attempt." >&2
  exit 1
}
[[ "$expected_archive_sha256" =~ ^[0-9a-f]{64}$ ]] || {
  echo "Invalid CI release-image archive checksum." >&2
  exit 1
}

api_image="greatsellai-hr-api:$release_commit"
caddy_image="greatsellai-hr-caddy:$release_commit"

archive="$(mktemp "/tmp/greatsell-ci-images-${release_commit}.XXXXXX")"
trap 'rm -f -- "$archive"' EXIT
cat > "$archive"
actual_archive_sha256="$(sha256sum "$archive" | awk '{print $1}')"
[[ "$actual_archive_sha256" == "$expected_archive_sha256" ]] || {
  echo "Transferred CI release-image archive checksum does not match." >&2
  exit 1
}
gzip -dc "$archive" | sudo -n docker image load
for image in "$api_image" "$caddy_image"; do
  revision="$(sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")" || {
    echo "Transferred image is unavailable: $image" >&2
    exit 1
  }
  [[ "$revision" == "$release_commit" ]] || {
    echo "Transferred image revision label does not match the release commit: $image" >&2
    exit 1
  }
  image_run_id="$(sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.workflow_run_id" }}' "$image")"
  [[ "$image_run_id" == "$expected_ci_run_id" ]] || {
    echo "Transferred image CI workflow run ID does not match: $image" >&2
    exit 1
  }
  image_run_attempt="$(sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.workflow_run_attempt" }}' "$image")"
  [[ "$image_run_attempt" == "$expected_ci_run_attempt" ]] || {
    echo "Transferred image CI workflow run attempt does not match: $image" >&2
    exit 1
  }
done
EOF
)"
remote_command="bash -c $(shell_quote "$remote_loader") -- $(shell_quote "$release_commit") $(shell_quote "$expected_ci_run_id") $(shell_quote "$expected_ci_run_attempt") $(shell_quote "$archive_sha256")"

echo "Streaming exact CI-verified release archive for $release_commit to $remote_host."
cat -- "$archive" | ssh "${ssh_options[@]}" "$remote_host" "$remote_command"
echo "Exact CI release archive transferred and images verified for $release_commit."
