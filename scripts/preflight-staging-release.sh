#!/usr/bin/env bash
# Validate a candidate main commit against the isolated staging environment.
# This is intentionally read-only: it neither transfers source/images nor
# starts, stops, builds, migrates, or removes a service.
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Usage: scripts/preflight-staging-release.sh <commit-sha> [options]

Options:
  --host <ssh-host>         Required SSH target (or RESUME_V3_DEPLOY_HOST)
  --project-dir <path>      Required staging directory (or RESUME_V3_REMOTE_DIR)
  --history-dir <path>      Required staging history directory (or RESUME_V3_DEPLOY_HISTORY_DIR)
  --ssh-key <path>          Optional SSH private-key path; never committed

The command renders the candidate's deploy/compose.staging.yml with the
server's existing .env.staging. It does not print environment values or touch
runtime services.
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
[[ "$project_dir" == /home/ubuntu/* && "$project_dir" == *staging* && "$project_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe staging project directory: $project_dir"
[[ "$history_dir" == /home/ubuntu/* && "$history_dir" == *staging* && "$history_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe staging history directory: $history_dir"
[[ "$project_dir" != "$history_dir" ]] || die "Staging project and history directories must be distinct."
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."

git fetch origin main --prune
git cat-file -e "$release_commit^{commit}" 2>/dev/null || die "Unknown release commit."
[[ "$release_commit" == "$(git rev-parse origin/main)" ]] || \
  die "Staging preflight accepts only the current origin/main commit."

ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=yes)
if [[ -n "$ssh_key" ]]; then
  ssh_options+=(-i "$ssh_key" -o IdentitiesOnly=yes)
fi

remote_preflight_script="$(cat <<'EOF'
set -Eeuo pipefail
project_dir="$1"
history_dir="$2"
release_commit="$3"

compose_content() {
  # Review the effective YAML lines only. A documentation comment mentioning a
  # forbidden production resource must not make staging preflight fail, while
  # an actual Compose reference must still fail closed.
  sed -E '/^[[:space:]]*#/d; s/[[:space:]]+#.*$//' "$1"
}

compose_has_line() {
  compose_content "$1" | grep -Fxq -- "$2"
}

compose_contains() {
  compose_content "$1" | grep -Fq -- "$2"
}

compose_matches() {
  compose_content "$1" | grep -Eq -- "$2"
}

umask 077
command -v realpath >/dev/null
canonical_project_dir="$(realpath -e -- "$project_dir")"
canonical_history_dir="$(realpath -m -- "$history_dir")"
[[ "$project_dir" == "$canonical_project_dir" ]] || { echo "Staging project path must not contain symlinks or traversal." >&2; exit 1; }
[[ "$history_dir" == "$canonical_history_dir" ]] || { echo "Staging history path must not contain symlinks or traversal." >&2; exit 1; }
project_dir="$canonical_project_dir"
history_dir="$canonical_history_dir"
[[ "$project_dir" == /home/ubuntu/* && "$project_dir" == *staging* && "$project_dir" != /home/ubuntu/ ]]
[[ "$history_dir" == /home/ubuntu/* && "$history_dir" == *staging* && "$history_dir" != /home/ubuntu/ ]]
[[ "$project_dir" != "$history_dir" ]]
test -f "$project_dir/.env.staging"
test ! -e "$project_dir/.env.production"
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
command -v flock >/dev/null
sudo -n docker compose version >/dev/null
temporary_compose="$(mktemp "/tmp/greatsell-staging-preflight-${release_commit}.XXXXXX")"
temporary_rendered="$(mktemp "/tmp/greatsell-staging-rendered-${release_commit}.XXXXXX")"
trap 'rm -f -- "$temporary_compose" "$temporary_rendered"' EXIT
cat > "$temporary_compose"
compose_has_line "$temporary_compose" 'name: resume-screening-v3-staging'
compose_contains "$temporary_compose" 'RESUME_V3_ENVIRONMENT: production'
compose_contains "$temporary_compose" '172.31.0.0/24'
compose_contains "$temporary_compose" '172.31.1.0/24'
compose_contains "$temporary_compose" '"172.17.0.1:18080:80"'
compose_contains "$temporary_compose" 'resume-screening-v3-staging_postgres_data'
compose_contains "$temporary_compose" 'resume-screening-v3-staging_uploads_data'
compose_contains "$temporary_compose" 'resume-screening-v3-staging_caddy_data'
compose_contains "$temporary_compose" 'resume-screening-v3-staging_caddy_config'
compose_contains "$temporary_compose" 'resume-screening-v3-staging_proxy'
compose_contains "$temporary_compose" 'resume-screening-v3-staging_backend'
! compose_matches "$temporary_compose" 'resume-screening-v3_(postgres_data|uploads_data|caddy_data|caddy_config|proxy|backend)'
! compose_contains "$temporary_compose" '.env.production'
! compose_matches "$temporary_compose" '(^|[^0-9])80:80([^0-9]|$)|(^|[^0-9])443:443([^0-9]|$)'
! compose_matches "$temporary_compose" '^[[:space:]]*build:'
sudo -n env "RESUME_V3_RELEASE_IMAGE_TAG=$release_commit" docker compose \
  --project-directory "$project_dir" \
  -f "$temporary_compose" \
  --env-file "$project_dir/.env.staging" config --quiet
sudo -n env "RESUME_V3_RELEASE_IMAGE_TAG=$release_commit" docker compose \
  --project-directory "$project_dir" \
  -f "$temporary_compose" \
  --env-file "$project_dir/.env.staging" config > "$temporary_rendered"
grep -Fqx 'name: resume-screening-v3-staging' "$temporary_rendered"
grep -Fq 'RESUME_V3_ENVIRONMENT: production' "$temporary_rendered"
grep -Fq 'host_ip: 172.17.0.1' "$temporary_rendered"
grep -Fq 'published: "18080"' "$temporary_rendered"
grep -Fq 'subnet: 172.31.0.0/24' "$temporary_rendered"
grep -Fq 'subnet: 172.31.1.0/24' "$temporary_rendered"
! grep -Eq 'resume-screening-v3_(postgres_data|uploads_data|caddy_data|caddy_config|proxy|backend)' "$temporary_rendered"
! grep -Eq 'published: "(80|443)"' "$temporary_rendered"
EOF
)"

git show "$release_commit:deploy/compose.staging.yml" | ssh "${ssh_options[@]}" "$remote_host" \
  "bash -c $(shell_quote "$remote_preflight_script") -- $(shell_quote "$project_dir") $(shell_quote "$history_dir") $(shell_quote "$release_commit")"

echo "Staging configuration preflight passed for $release_commit."
