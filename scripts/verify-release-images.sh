#!/usr/bin/env bash
# Verify CI-attested images on a promotion target without comparing
# daemon-local image IDs across different Docker implementations.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/verify-release-images.sh <commit-sha> [options]

Options:
  --host <ssh-host>         Required production SSH target
  --expected-ci-run-id <id> Required CI workflow run ID attested by staging
  --expected-ci-run-attempt <number>
                            Required CI workflow run attempt attested by staging
  --ssh-key <path>          Optional SSH private-key path; never committed

This command does not transfer, build, tag, or deploy images. It fails closed
unless the promotion target holds images with the completed staging candidate's
immutable revision and CI provenance labels.
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
expected_ci_run_id=""
expected_ci_run_attempt=""
ssh_key=""
while (($#)); do
  case "$1" in
    --host) remote_host="${2:?--host requires a value}"; shift 2 ;;
    --expected-ci-run-id) expected_ci_run_id="${2:?--expected-ci-run-id requires a value}"; shift 2 ;;
    --expected-ci-run-attempt) expected_ci_run_attempt="${2:?--expected-ci-run-attempt requires a value}"; shift 2 ;;
    --ssh-key) ssh_key="${2:?--ssh-key requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$remote_host" ]] || die "Missing promotion target; pass --host."
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

platform="$(sudo -n docker version --format '{{.Server.Os}}/{{.Server.Arch}}')" || {
  echo "Unable to inspect promotion Docker platform." >&2
  exit 1
}
[[ "$platform" == "linux/amd64" ]] || {
  echo "Promotion target platform must be linux/amd64; got $platform." >&2
  exit 1
}

require_image() {
  local image="$1" observed_id revision ci_run_id ci_run_attempt
  observed_id="$(sudo -n docker image inspect --format '{{.Id}}' "$image")" || {
    echo "Promotion image is unavailable: $image" >&2
    exit 1
  }
  revision="$(sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")"
  [[ "$observed_id" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "Promotion image has an invalid local image ID: $image" >&2
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

require_image "greatsellai-hr-api:$release_commit"
require_image "greatsellai-hr-caddy:$release_commit"
EOF
)"

ssh "${ssh_options[@]}" "$remote_host" \
  "bash -c $(shell_quote "$remote_verify_script") -- $(shell_quote "$release_commit") $(shell_quote "$expected_ci_run_id") $(shell_quote "$expected_ci_run_attempt")"

echo "Promotion target holds CI-attested images for $release_commit."
