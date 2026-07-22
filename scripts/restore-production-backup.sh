#!/usr/bin/env bash
# Restore a verified, paired PostgreSQL + uploads_data release backup.
#
# This command intentionally does not deploy application code. First roll back
# or redeploy the reviewed application tag that is known to be compatible with
# the selected database snapshot, then run this explicit, confirmed data
# restore. The remote helper takes a fresh pre-restore safety backup before it
# changes either store.
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Usage: scripts/restore-production-backup.sh <prod-tag> <backup-id> [options]

Options:
  --host <ssh-host>         Required SSH target (or RESUME_V3_DEPLOY_HOST)
  --project-dir <path>      Required live project directory (or RESUME_V3_REMOTE_DIR)
  --history-dir <path>      Required release-record/backup directory (or RESUME_V3_DEPLOY_HISTORY_DIR)
  --ssh-key <path>          Optional SSH private-key path; never committed
  --confirm-restore         Required acknowledgement for the destructive restore

The current reviewed deployment tooling verifies both backup artifacts and
takes a new safety backup before replacing PostgreSQL or uploads_data. The
selected prod tag is still verified as a reachable, immutable application
release. The command never transfers .env.production, candidate files, or
secrets.
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

release_tag="${1:-}"
backup_id="${2:-}"
[[ -n "$release_tag" && "$release_tag" != -* && -n "$backup_id" && "$backup_id" != -* ]] || {
  usage >&2
  exit 1
}
shift 2

remote_host="${RESUME_V3_DEPLOY_HOST:-}"
project_dir="${RESUME_V3_REMOTE_DIR:-}"
history_dir="${RESUME_V3_DEPLOY_HISTORY_DIR:-}"
ssh_key="${RESUME_V3_SSH_KEY:-}"
confirmed=0

while (($#)); do
  case "$1" in
    --host) remote_host="${2:?--host requires a value}"; shift 2 ;;
    --project-dir) project_dir="${2:?--project-dir requires a value}"; shift 2 ;;
    --history-dir) history_dir="${2:?--history-dir requires a value}"; shift 2 ;;
    --ssh-key) ssh_key="${2:?--ssh-key requires a value}"; shift 2 ;;
    --confirm-restore) confirmed=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ "$release_tag" =~ ^prod-[0-9]{8}-[0-9a-f]{7,40}$ ]] || die "Invalid production tag: $release_tag"
[[ "$backup_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$ ]] || die "Invalid backup ID."
[[ "$confirmed" -eq 1 ]] || die "Pass --confirm-restore to acknowledge the destructive restore."
[[ -n "$remote_host" ]] || die "Missing deployment target; pass --host or set RESUME_V3_DEPLOY_HOST."
[[ -n "$project_dir" ]] || die "Missing project directory; pass --project-dir or set RESUME_V3_REMOTE_DIR."
[[ -n "$history_dir" ]] || die "Missing history directory; pass --history-dir or set RESUME_V3_DEPLOY_HISTORY_DIR."
[[ "$project_dir" == /home/ubuntu/* && "$project_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe project directory: $project_dir"
[[ "$history_dir" == /home/ubuntu/* && "$history_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe history directory: $history_dir"
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."

git fetch origin main --tags --prune
release_commit="$(git rev-parse -q --verify "refs/tags/$release_tag^{commit}")" || \
  die "Unknown local tag: $release_tag"
[[ "$release_commit" == "${release_tag##*-}"* ]] || \
  die "Tag suffix does not match its target commit."
git ls-remote --exit-code --tags origin "refs/tags/$release_tag" >/dev/null 2>&1 || \
  die "Tag '$release_tag' has not been pushed to GitHub."
git merge-base --is-ancestor "$release_commit" origin/main || \
  die "Tag '$release_tag' is not reachable from origin/main."

ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
if [[ -n "$ssh_key" ]]; then
  ssh_options+=(-i "$ssh_key" -o IdentitiesOnly=yes)
fi

ssh_run() {
  ssh "${ssh_options[@]}" "$remote_host" "$@"
}

remote_helper="/tmp/greatsell-restore-${release_tag}-${backup_id}.sh"
cleanup_remote_helper() {
  ssh_run "rm -f $(shell_quote "$remote_helper")" >/dev/null 2>&1 || true
}
trap cleanup_remote_helper EXIT

cat "$repo_root/scripts/remote-release-helper.sh" | ssh "${ssh_options[@]}" "$remote_host" \
  "umask 077 && cat > $(shell_quote "$remote_helper") && chmod 700 $(shell_quote "$remote_helper")"
ssh_run "bash $(shell_quote "$remote_helper") restore $(shell_quote "$project_dir") $(shell_quote "$history_dir") $(shell_quote "$backup_id") RESTORE"

echo "Restore succeeded from backup $backup_id using $release_tag ($release_commit)."
