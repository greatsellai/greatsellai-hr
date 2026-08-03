#!/usr/bin/env bash
# Import one verified production data snapshot into an otherwise fresh
# production host. The bundle must already be present on that host under the
# protected release-history incoming directory; this wrapper never transfers
# an environment file, credentials, candidate files, or Docker volumes.
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap-import-production.sh <import-id> [options]

Options:
  --host <ssh-host>         Required SSH target (or RESUME_V3_DEPLOY_HOST)
  --project-dir <path>      Required production environment directory (or RESUME_V3_REMOTE_DIR)
  --history-dir <path>      Required production release-history directory (or RESUME_V3_DEPLOY_HISTORY_DIR)
  --ssh-key <path>          Optional SSH private-key path; never committed
  --confirm-import          Required acknowledgement for importing production data

Before running this command, place exactly one verified portable bundle at:
  <history-dir>/incoming/<import-id>/

The bundle contains only database.dump, uploads.tar.gz, checksums.sha256, and
manifest.env. The remote helper rejects all non-production source metadata,
existing production release state, all pre-existing target volumes, and any
missing acknowledgement. It never copies, reads, or prints .env.production.
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

import_id="${1:-}"
[[ -n "$import_id" && "$import_id" != -* ]] || {
  usage >&2
  exit 1
}
shift

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
    --confirm-import) confirmed=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ "$import_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$ ]] || die "Invalid production bootstrap import ID."
[[ "$confirmed" -eq 1 ]] || die "Pass --confirm-import to acknowledge production data import."
[[ -n "$remote_host" ]] || die "Missing deployment target; pass --host or set RESUME_V3_DEPLOY_HOST."
[[ -n "$project_dir" ]] || die "Missing project directory; pass --project-dir or set RESUME_V3_REMOTE_DIR."
[[ -n "$history_dir" ]] || die "Missing history directory; pass --history-dir or set RESUME_V3_DEPLOY_HISTORY_DIR."
[[ "$project_dir" == /home/ubuntu/* && "$project_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe project directory: $project_dir"
[[ "$history_dir" == /home/ubuntu/* && "$history_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe history directory: $history_dir"
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."

if [[ -n "$(git status --porcelain)" ]]; then
  die "Refusing bootstrap import from a dirty local worktree. Commit or stash changes first."
fi
git fetch origin main --prune
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || \
  die "Run the bootstrap importer from the current reviewed origin/main commit."

ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=yes)
if [[ -n "$ssh_key" ]]; then
  ssh_options+=(-i "$ssh_key" -o IdentitiesOnly=yes)
fi

ssh_run() {
  ssh "${ssh_options[@]}" "$remote_host" "$@"
}

remote_helper="/tmp/greatsell-bootstrap-import-${import_id}.sh"
remote_validator="/tmp/greatsell-bootstrap-validator-${import_id}.py"
cleanup_remote_tools() {
  ssh_run "rm -f $(shell_quote "$remote_helper") $(shell_quote "$remote_validator")" >/dev/null 2>&1 || true
}
trap cleanup_remote_tools EXIT

cat "$repo_root/scripts/remote-release-helper.sh" | ssh "${ssh_options[@]}" "$remote_host" \
  "umask 077 && cat > $(shell_quote "$remote_helper") && chmod 700 $(shell_quote "$remote_helper")"
cat "$repo_root/scripts/validate_production_bootstrap_bundle.py" | ssh "${ssh_options[@]}" "$remote_host" \
  "umask 077 && cat > $(shell_quote "$remote_validator") && chmod 700 $(shell_quote "$remote_validator")"
ssh_run "bash $(shell_quote "$remote_helper") bootstrap-import $(shell_quote "$project_dir") $(shell_quote "$history_dir") $(shell_quote "$import_id") IMPORT_PRODUCTION_SNAPSHOT $(shell_quote "$remote_validator")"

echo "Production bootstrap import completed for $import_id."
