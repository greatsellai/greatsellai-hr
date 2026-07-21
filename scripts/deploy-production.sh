#!/usr/bin/env bash
# Deploy only a GitHub production tag. Production data and environment files are
# intentionally never part of the transfer.
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Usage: scripts/deploy-production.sh <prod-tag> [options]

Options:
  --host <ssh-host>         Required SSH target (or RESUME_V3_DEPLOY_HOST)
  --project-dir <path>      Required live project directory (or RESUME_V3_REMOTE_DIR)
  --history-dir <path>      Required release-record/backup directory (or RESUME_V3_DEPLOY_HISTORY_DIR)
  --ssh-key <path>          Optional SSH private-key path; never committed
  --rollback                Deploy a prior tag as an application rollback
  --allow-schema-ahead      Required for rollback across migration changes;
                            keeps the current database schema and skips migrate

Only prod-YYYYMMDD-<commit-sha> tags that are reachable from origin/main are
accepted. The command transfers tracked source with git archive; it never uses
--delete and cannot transfer .env.production, Docker volumes, PDFs, or database data.
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

tag="${1:-}"
[[ -n "$tag" && "$tag" != -* ]] || { usage >&2; exit 1; }
shift

remote_host="${RESUME_V3_DEPLOY_HOST:-}"
project_dir="${RESUME_V3_REMOTE_DIR:-}"
history_dir="${RESUME_V3_DEPLOY_HISTORY_DIR:-}"
ssh_key="${RESUME_V3_SSH_KEY:-}"
mode="deploy"
allow_schema_ahead=0

while (($#)); do
  case "$1" in
    --host) remote_host="${2:?--host requires a value}"; shift 2 ;;
    --project-dir) project_dir="${2:?--project-dir requires a value}"; shift 2 ;;
    --history-dir) history_dir="${2:?--history-dir requires a value}"; shift 2 ;;
    --ssh-key) ssh_key="${2:?--ssh-key requires a value}"; shift 2 ;;
    --rollback) mode="rollback"; shift ;;
    --allow-schema-ahead) allow_schema_ahead=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ "$tag" =~ ^prod-[0-9]{8}-[0-9a-f]{7,40}$ ]] || die "Invalid production tag: $tag"
[[ -n "$remote_host" ]] || die "Missing deployment target; pass --host or set RESUME_V3_DEPLOY_HOST."
[[ -n "$project_dir" ]] || die "Missing project directory; pass --project-dir or set RESUME_V3_REMOTE_DIR."
[[ -n "$history_dir" ]] || die "Missing history directory; pass --history-dir or set RESUME_V3_DEPLOY_HISTORY_DIR."
[[ "$project_dir" == /home/ubuntu/* && "$project_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe project directory: $project_dir"
[[ "$history_dir" == /home/ubuntu/* && "$history_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe history directory: $history_dir"
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."

if [[ -n "$(git status --porcelain)" ]]; then
  die "Refusing deployment from a dirty local worktree. Commit or stash changes first."
fi

git fetch origin main --tags --prune
release_commit="$(git rev-parse -q --verify "refs/tags/$tag^{commit}")" || die "Unknown local tag: $tag"
tag_short_commit="${tag##*-}"
[[ "$release_commit" == "$tag_short_commit"* ]] || \
  die "Tag '$tag' suffix does not match its target commit."
git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1 || \
  die "Tag '$tag' has not been pushed to GitHub."
git merge-base --is-ancestor "$release_commit" origin/main || \
  die "Tag '$tag' is not reachable from origin/main."

ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
if [[ -n "$ssh_key" ]]; then
  ssh_options+=(-i "$ssh_key" -o IdentitiesOnly=yes)
fi

ssh_run() {
  ssh "${ssh_options[@]}" "$remote_host" "$@"
}

remote_current="$(ssh_run "if [ -f $(shell_quote "$history_dir/current-release.env") ]; then sed -n -e 's/^tag=//p' -e 's/^commit=//p' $(shell_quote "$history_dir/current-release.env"); fi")"
previous_tag=""
previous_commit=""
while IFS= read -r line; do
  if [[ "$line" =~ ^prod-[0-9]{8}-[0-9a-f]{7,40}$ ]]; then
    previous_tag="$line"
  elif [[ "$line" =~ ^[0-9a-f]{40}$ ]]; then
    previous_commit="$line"
  fi
done <<< "$remote_current"

if [[ "$mode" == "rollback" ]]; then
  published_target="$(ssh_run "if [ -d $(shell_quote "$history_dir/releases") ] && grep -R -q -- $(shell_quote "^tag=$tag$") $(shell_quote "$history_dir/releases"); then printf yes; fi")"
  [[ "$published_target" == "yes" ]] || \
    die "Rollback target '$tag' has no successful production deployment record."
fi

migration_changed=0
if [[ -z "$previous_commit" ]]; then
  migration_changed=1
elif ! git cat-file -e "$previous_commit^{commit}" 2>/dev/null; then
  die "The recorded production commit is not available locally; fetch its release tag before deploying."
elif [[ -n "$(git diff --name-only "$previous_commit" "$release_commit" -- migrations/)" ]]; then
  migration_changed=1
fi

if [[ "$mode" == "rollback" && "$migration_changed" -eq 1 && "$allow_schema_ahead" -ne 1 ]]; then
  die "Rollback crosses migration changes. Review schema compatibility and retry with --allow-schema-ahead; this does not downgrade the database."
fi
if [[ "$mode" != "rollback" && "$allow_schema_ahead" -eq 1 ]]; then
  die "--allow-schema-ahead is only valid for an explicit rollback."
fi

# A release can change application behavior even without a migration. Capture
# PostgreSQL and the shared originals volume as one verified bundle before
# every deploy or rollback, rather than treating schema changes as the only
# moment when recoverability matters.
backup_required=1

previous_tag_arg="${previous_tag:-__none__}"
previous_commit_arg="${previous_commit:-__none__}"
remote_helper="/tmp/greatsell-release-${tag}.sh"
remote_compose="/tmp/greatsell-compose-${tag}.yml"

git show "$tag:scripts/remote-release-helper.sh" | ssh "${ssh_options[@]}" "$remote_host" \
  "umask 077 && cat > $(shell_quote "$remote_helper") && chmod 700 $(shell_quote "$remote_helper")"

# Render the tagged Compose file against the server's existing environment
# before quiescing data services or transferring any source. This catches a
# newly required non-secret deployment setting (for example the fixed Caddy
# trusted-proxy CIDR) while the previous release remains fully available.
git show "$tag:compose.yml" | ssh "${ssh_options[@]}" "$remote_host" \
  "umask 077; temp=$(shell_quote "$remote_compose"); trap 'rm -f -- \"$remote_compose\"' EXIT; cat > \"$remote_compose\"; sudo -n env RESUME_V3_RELEASE_IMAGE_TAG=$(shell_quote "$release_commit") docker compose --project-directory $(shell_quote "$project_dir") -f \"$remote_compose\" --env-file $(shell_quote "$project_dir/.env.production") config --quiet"
remote_release() {
  ssh_run "bash $(shell_quote "$remote_helper") $(shell_quote "$1") $(shell_quote "$project_dir") $(shell_quote "$history_dir") $(shell_quote "$tag") $(shell_quote "$release_commit") $(shell_quote "$previous_tag_arg") $(shell_quote "$previous_commit_arg") $(shell_quote "$mode") $(shell_quote "$2")"
}
remote_release precheck "$backup_required"

# git archive contains tracked source only. It cannot include ignored production
# secrets or data, and extraction intentionally does not delete unknown files.
git archive --format=tar "$tag" | ssh "${ssh_options[@]}" "$remote_host" \
  "tar -x -C $(shell_quote "$project_dir")"

skip_migrate=0
if [[ "$mode" == "rollback" && "$migration_changed" -eq 1 ]]; then
  skip_migrate=1
fi

remote_release deploy "$skip_migrate"
ssh_run "rm -f $(shell_quote "$remote_helper")"

echo "Deployment succeeded: $tag ($release_commit)"
