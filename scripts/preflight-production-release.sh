#!/usr/bin/env bash
# Validate a candidate main commit against the server's existing production
# environment without creating a tag, transferring source, or touching runtime
# services. This intentionally catches missing required Compose variables
# before the automatic release workflow creates a prod-* tag.
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Usage: scripts/preflight-production-release.sh <commit-sha> [options]

Options:
  --host <ssh-host>         Required SSH target (or RESUME_V3_DEPLOY_HOST)
  --project-dir <path>      Required environment directory (or RESUME_V3_REMOTE_DIR)
  --history-dir <path>      Required release-history directory (or RESUME_V3_DEPLOY_HISTORY_DIR)
  --ssh-key <path>          Optional SSH private-key path; never committed

The command renders the candidate commit's Compose model with the server's
existing .env.production. It does not create a tag, transfer source, stop a
service, build an image, run a migration, or print environment values.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

shell_quote() {
  printf '%q' "$1"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

release_commit="${1:-}"
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || {
  usage >&2
  exit 1
}
shift

remote_host="${RESUME_V3_DEPLOY_HOST:-}"
project_dir="${RESUME_V3_REMOTE_DIR:-}"
history_dir="${RESUME_V3_DEPLOY_HISTORY_DIR:-}"
ssh_key="${RESUME_V3_SSH_KEY:-}"

while (($#)); do
  case "$1" in
    --host) remote_host="${2:?--host requires a value}"; shift 2 ;;
    --project-dir) project_dir="${2:?--project-dir requires a value}"; shift 2 ;;
    --history-dir) history_dir="${2:?--history-dir requires a value}"; shift 2 ;;
    --ssh-key) ssh_key="${2:?--ssh-key requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$remote_host" ]] || die "Missing deployment target; pass --host or set RESUME_V3_DEPLOY_HOST."
[[ -n "$project_dir" ]] || die "Missing project directory; pass --project-dir or set RESUME_V3_REMOTE_DIR."
[[ -n "$history_dir" ]] || die "Missing history directory; pass --history-dir or set RESUME_V3_DEPLOY_HISTORY_DIR."
[[ "$project_dir" == /home/ubuntu/* && "$project_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe project directory: $project_dir"
[[ "$history_dir" == /home/ubuntu/* && "$history_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe history directory: $history_dir"
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."

git fetch origin main --prune
git cat-file -e "$release_commit^{commit}" 2>/dev/null || die "Unknown release commit."
[[ "$release_commit" == "$(git rev-parse origin/main)" ]] || \
  die "Release preflight accepts only the current origin/main commit."

ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
if [[ -n "$ssh_key" ]]; then
  ssh_options+=(-i "$ssh_key" -o IdentitiesOnly=yes)
fi

remote_preflight_script="$(cat <<'EOF'
set -Eeuo pipefail
project_dir="$1"
history_dir="$2"
release_commit="$3"

umask 077
test -d "$project_dir"
test -f "$project_dir/.env.production"
if [[ -e "$history_dir" ]]; then
  test -d "$history_dir"
  test -w "$history_dir"
  test -x "$history_dir"
else
  history_parent="$(dirname -- "$history_dir")"
  while [[ ! -e "$history_parent" && "$history_parent" != / ]]; do
    history_parent="$(dirname -- "$history_parent")"
  done
  test -d "$history_parent"
  test -w "$history_parent"
  test -x "$history_parent"
fi
command -v python3 >/dev/null
command -v flock >/dev/null
sudo -n docker compose version >/dev/null
temporary_compose="$(mktemp "/tmp/greatsell-preflight-${release_commit}.XXXXXX")"
trap 'rm -f -- "$temporary_compose"' EXIT
cat > "$temporary_compose"
sudo -n env "RESUME_V3_RELEASE_IMAGE_TAG=$release_commit" docker compose --project-directory "$project_dir" -f "$temporary_compose" --env-file "$project_dir/.env.production" config --quiet
EOF
)"

git show "$release_commit:compose.yml" | ssh "${ssh_options[@]}" "$remote_host" \
  "bash -c $(shell_quote "$remote_preflight_script") -- $(shell_quote "$project_dir") $(shell_quote "$history_dir") $(shell_quote "$release_commit")"

echo "Production configuration preflight passed for $release_commit."
