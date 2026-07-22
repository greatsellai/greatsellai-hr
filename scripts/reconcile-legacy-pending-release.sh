#!/usr/bin/env bash
# Reconcile one known, interrupted legacy pending release without deploying code.
# It makes a fresh verified PostgreSQL + uploads snapshot before archiving the
# marker, so the next normal release can proceed without blindly overwriting it.
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Usage: scripts/reconcile-legacy-pending-release.sh <pending-prod-tag> <pending-commit> [options]

Options:
  --host <ssh-host>         Required SSH target (or RESUME_V3_DEPLOY_HOST)
  --project-dir <path>      Required live project directory (or RESUME_V3_REMOTE_DIR)
  --history-dir <path>      Required release-record/backup directory (or RESUME_V3_DEPLOY_HISTORY_DIR)
  --ssh-key <path>          Optional SSH private-key path; never committed
  --confirm                 Required acknowledgement for this recovery action

This command is for one interrupted legacy deployment only. It requires the
exact pending tag and 40-character commit, verifies that the record's recorded
predecessor is still current, requires every app writer to be stopped, takes a
new paired PostgreSQL + uploads backup, then archives the pending marker. It
does not deploy, build, migrate, restart services, read .env.production, or
delete candidate data.
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

pending_tag="${1:-}"
pending_commit="${2:-}"
[[ -n "$pending_tag" && "$pending_tag" != -* && -n "$pending_commit" && "$pending_commit" != -* ]] || {
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
    --confirm) confirmed=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ "$pending_tag" =~ ^prod-[0-9]{8}-[0-9a-f]{7,40}$ ]] || die "Invalid pending production tag."
[[ "$pending_commit" =~ ^[0-9a-f]{40}$ ]] || die "Invalid pending production commit."
[[ "$confirmed" -eq 1 ]] || die "Pass --confirm to acknowledge legacy pending-release reconciliation."
[[ -n "$remote_host" ]] || die "Missing deployment target; pass --host or set RESUME_V3_DEPLOY_HOST."
[[ -n "$project_dir" ]] || die "Missing project directory; pass --project-dir or set RESUME_V3_REMOTE_DIR."
[[ -n "$history_dir" ]] || die "Missing history directory; pass --history-dir or set RESUME_V3_DEPLOY_HISTORY_DIR."
[[ "$project_dir" == /home/ubuntu/* && "$project_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe project directory: $project_dir"
[[ "$history_dir" == /home/ubuntu/* && "$history_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe history directory: $history_dir"
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."

if [[ -n "$(git status --porcelain)" ]]; then
  die "Refusing recovery from a dirty local worktree. Commit or stash changes first."
fi

git fetch origin main --tags --prune
tag_commit="$(git rev-parse -q --verify "refs/tags/$pending_tag^{commit}")" || \
  die "Unknown local pending production tag: $pending_tag"
[[ "$tag_commit" == "$pending_commit" ]] || \
  die "Pending tag does not resolve to the exact operator-confirmed commit."
git ls-remote --exit-code --tags origin "refs/tags/$pending_tag" >/dev/null 2>&1 || \
  die "Pending production tag has not been pushed to GitHub."
git merge-base --is-ancestor "$pending_commit" origin/main || \
  die "Pending production tag is not reachable from origin/main."

ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=yes)
if [[ -n "$ssh_key" ]]; then
  ssh_options+=(-i "$ssh_key" -o IdentitiesOnly=yes)
fi

ssh_run() {
  ssh "${ssh_options[@]}" "$remote_host" "$@"
}

remote_helper="$(ssh_run 'umask 077 && mktemp /tmp/greatsell-legacy-reconcile.XXXXXXXX')"
[[ "$remote_helper" =~ ^/tmp/greatsell-legacy-reconcile\.[A-Za-z0-9]{8}$ ]] || \
  die "Remote helper path was not created safely."
cat "$repo_root/scripts/remote-release-helper.sh" | ssh "${ssh_options[@]}" "$remote_host" \
  "cat > $(shell_quote "$remote_helper") && chmod 700 $(shell_quote "$remote_helper")"
cleanup_remote_helper() {
  ssh_run "rm -f $(shell_quote "$remote_helper")" >/dev/null 2>&1 || true
}
trap cleanup_remote_helper EXIT

ssh_run "bash $(shell_quote "$remote_helper") reconcile-legacy-pending $(shell_quote "$project_dir") $(shell_quote "$history_dir") $(shell_quote "$pending_tag") $(shell_quote "$pending_commit") RECONCILE_LEGACY_PENDING"

echo "Legacy pending release reconciled: $pending_tag ($pending_commit)"
