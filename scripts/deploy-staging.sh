#!/usr/bin/env bash
# Deploy a reviewed stg-* candidate into the isolated staging Compose project.
# Staging source/configuration never shares a directory, volume, network, or
# environment file with production.
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Usage: scripts/deploy-staging.sh <stg-tag> [options]

Options:
  --host <ssh-host>         Required SSH target (or RESUME_V3_DEPLOY_HOST)
  --project-dir <path>      Required isolated staging directory (or RESUME_V3_REMOTE_DIR)
  --history-dir <path>      Required staging release-history directory (or RESUME_V3_DEPLOY_HISTORY_DIR)
  --public-url <url>        Required staging public URL for post-deploy smoke checks
  --ci-image-archive-sha256 <hash>
                            Required SHA-256 of the verified CI image archive
  --api-image-config-digest <sha256>
                            Required portable API image config identity
  --caddy-image-config-digest <sha256>
                            Required portable Caddy image config identity
  --ssh-key <path>          Optional SSH private-key path; never committed

Only pushed stg-YYYYMMDD-<commit-sha> tags that exactly match current
origin/main are accepted. API and Caddy images must already have been
transferred from CI and are never built on the server.
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
public_url="${RESUME_V3_STAGING_PUBLIC_URL:-}"
ci_image_archive_sha256=""
api_image_config_digest=""
caddy_image_config_digest=""
ssh_key="${RESUME_V3_SSH_KEY:-}"

while (($#)); do
  case "$1" in
    --host) remote_host="${2:?--host requires a value}"; shift 2 ;;
    --project-dir) project_dir="${2:?--project-dir requires a value}"; shift 2 ;;
    --history-dir) history_dir="${2:?--history-dir requires a value}"; shift 2 ;;
    --public-url) public_url="${2:?--public-url requires a value}"; shift 2 ;;
    --ci-image-archive-sha256) ci_image_archive_sha256="${2:?--ci-image-archive-sha256 requires a value}"; shift 2 ;;
    --api-image-config-digest) api_image_config_digest="${2:?--api-image-config-digest requires a value}"; shift 2 ;;
    --caddy-image-config-digest) caddy_image_config_digest="${2:?--caddy-image-config-digest requires a value}"; shift 2 ;;
    --ssh-key) ssh_key="${2:?--ssh-key requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ "$tag" =~ ^stg-[0-9]{8}-[0-9a-f]{7,40}$ ]] || die "Invalid staging tag: $tag"
[[ -n "$remote_host" ]] || die "Missing deployment target; pass --host or set RESUME_V3_DEPLOY_HOST."
[[ -n "$project_dir" ]] || die "Missing project directory; pass --project-dir or set RESUME_V3_REMOTE_DIR."
[[ -n "$history_dir" ]] || die "Missing history directory; pass --history-dir or set RESUME_V3_DEPLOY_HISTORY_DIR."
[[ -n "$public_url" ]] || die "Missing public staging URL; pass --public-url or set RESUME_V3_STAGING_PUBLIC_URL."
[[ "$ci_image_archive_sha256" =~ ^[0-9a-f]{64}$ ]] || die "Missing or invalid verified CI image archive checksum."
[[ "$api_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Missing or invalid API image config digest."
[[ "$caddy_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Missing or invalid Caddy image config digest."
[[ "$project_dir" == /home/ubuntu/* && "$project_dir" == *staging* && "$project_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe staging project directory: $project_dir"
[[ "$history_dir" == /home/ubuntu/* && "$history_dir" == *staging* && "$history_dir" != /home/ubuntu/ ]] || \
  die "Refusing unsafe staging history directory: $history_dir"
[[ "$project_dir" != "$history_dir" ]] || die "Staging project and history directories must be distinct."
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."

if [[ -n "$(git status --porcelain)" ]]; then
  die "Refusing deployment from a dirty local worktree. Commit changes first."
fi

git fetch origin main --tags --prune
release_commit="$(git rev-parse -q --verify "refs/tags/$tag^{commit}")" || die "Unknown local tag: $tag"
tag_short_commit="${tag##*-}"
[[ "$release_commit" == "$tag_short_commit"* ]] || die "Tag suffix does not match its target commit."
git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1 || \
  die "Staging tag '$tag' has not been pushed to GitHub."
[[ "$release_commit" == "$(git rev-parse origin/main)" ]] || \
  die "Staging candidates must match the current origin/main commit."

archive_sha256="$(git archive --format=tar "$tag" | sha256sum | awk '{print $1}')"
[[ "$archive_sha256" =~ ^[0-9a-f]{64}$ ]] || die "Unable to checksum staging candidate source."

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

remote_deploy_script="$(cat <<'EOF'
set -Eeuo pipefail
project_dir="$1"
history_dir="$2"
tag="$3"
release_commit="$4"
archive_sha256="$5"
ci_image_archive_sha256="$6"
api_image_config_digest="$7"
caddy_image_config_digest="$8"

die() {
  echo "Staging deployment error: $*" >&2
  exit 1
}

compose_content() {
  # Review the effective YAML lines only. A documentation comment mentioning a
  # forbidden production resource must not make staging deployment fail, while
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

record_value() {
  sed -n "s/^$2=//p" "$1" | tail -n 1
}

require_image() {
  local image="$1" expected_id actual_id revision
  expected_id="$2"
  actual_id="$(sudo -n docker image inspect --format '{{.Id}}' "$image")" || die "Required CI image is unavailable: $image"
  revision="$(sudo -n docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")"
  [[ "$revision" == "$release_commit" ]] || die "CI image revision does not match staging commit: $image"
  [[ -z "$expected_id" || "$actual_id" == "$expected_id" ]] || die "CI image identity changed unexpectedly: $image"
  printf '%s' "$actual_id"
}

[[ "$tag" =~ ^stg-[0-9]{8}-[0-9a-f]{7,40}$ ]] || die "Invalid staging tag."
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || die "Invalid staging commit."
[[ "$archive_sha256" =~ ^[0-9a-f]{64}$ ]] || die "Invalid staging archive checksum."
[[ "$ci_image_archive_sha256" =~ ^[0-9a-f]{64}$ ]] || die "Invalid CI image archive checksum."
[[ "$api_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid API image config digest."
[[ "$caddy_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Invalid Caddy image config digest."
command -v realpath >/dev/null
canonical_project_dir="$(realpath -e -- "$project_dir")"
canonical_history_dir="$(realpath -m -- "$history_dir")"
[[ "$project_dir" == "$canonical_project_dir" ]] || die "Staging project path must not contain symlinks or traversal."
[[ "$history_dir" == "$canonical_history_dir" ]] || die "Staging history path must not contain symlinks or traversal."
project_dir="$canonical_project_dir"
history_dir="$canonical_history_dir"
[[ "$project_dir" == /home/ubuntu/* && "$project_dir" == *staging* && "$project_dir" != /home/ubuntu/ ]] || die "Unsafe staging project directory."
[[ "$history_dir" == /home/ubuntu/* && "$history_dir" == *staging* && "$history_dir" != /home/ubuntu/ ]] || die "Unsafe staging history directory."
[[ "$project_dir" != "$history_dir" ]] || die "Staging project and history directories must be distinct."
test -d "$project_dir"
test -f "$project_dir/.env.staging"
test ! -e "$project_dir/.env.production"
sudo -n docker compose version >/dev/null

umask 077
mkdir -p "$history_dir/releases"
chmod 700 "$history_dir" "$history_dir/releases"
exec 9>"$history_dir/.staging-release.lock"
flock -n 9 || die "Another staging deployment is already running."

candidate_compose="$(mktemp "/tmp/greatsell-staging-${release_commit}.XXXXXX")"
rendered_compose="$(mktemp "/tmp/greatsell-staging-rendered-${release_commit}.XXXXXX")"
trap 'rm -f -- "$candidate_compose" "$rendered_compose"' EXIT
cat > "$candidate_compose"
compose_has_line "$candidate_compose" 'name: resume-screening-v3-staging' || die "Unexpected staging Compose project name."
compose_contains "$candidate_compose" 'RESUME_V3_ENVIRONMENT: production' || die "Staging application must run in production mode."
compose_contains "$candidate_compose" '172.31.0.0/24' || die "Staging proxy subnet is missing."
compose_contains "$candidate_compose" '172.31.1.0/24' || die "Staging backend subnet is missing."
compose_contains "$candidate_compose" '"172.17.0.1:18080:80"' || die "Staging private edge binding is missing."
compose_contains "$candidate_compose" 'resume-screening-v3-staging_postgres_data' || die "Staging database volume is missing."
compose_contains "$candidate_compose" 'resume-screening-v3-staging_uploads_data' || die "Staging upload volume is missing."
compose_contains "$candidate_compose" 'resume-screening-v3-staging_caddy_data' || die "Staging Caddy data volume is missing."
compose_contains "$candidate_compose" 'resume-screening-v3-staging_caddy_config' || die "Staging Caddy config volume is missing."
compose_contains "$candidate_compose" 'resume-screening-v3-staging_proxy' || die "Staging proxy network is missing."
compose_contains "$candidate_compose" 'resume-screening-v3-staging_backend' || die "Staging backend network is missing."
! compose_matches "$candidate_compose" 'resume-screening-v3_(postgres_data|uploads_data|caddy_data|caddy_config|proxy|backend)' || die "Candidate Compose references a production resource."
! compose_contains "$candidate_compose" '.env.production' || die "Candidate Compose references a production environment file."
! compose_matches "$candidate_compose" '(^|[^0-9])80:80([^0-9]|$)|(^|[^0-9])443:443([^0-9]|$)' || die "Candidate Compose publishes a production public port."
! compose_matches "$candidate_compose" '^[[:space:]]*build:' || die "Staging Compose must only use CI-built images."

sudo -n env "RESUME_V3_RELEASE_IMAGE_TAG=$release_commit" docker compose \
  --project-directory "$project_dir" \
  -f "$candidate_compose" \
  --env-file "$project_dir/.env.staging" config --quiet
sudo -n env "RESUME_V3_RELEASE_IMAGE_TAG=$release_commit" docker compose \
  --project-directory "$project_dir" \
  -f "$candidate_compose" \
  --env-file "$project_dir/.env.staging" config > "$rendered_compose"
grep -Fqx 'name: resume-screening-v3-staging' "$rendered_compose" || die "Rendered Compose project name changed unexpectedly."
grep -Fq 'RESUME_V3_ENVIRONMENT: production' "$rendered_compose" || die "Rendered Compose lost production application mode."
grep -Fq 'host_ip: 172.17.0.1' "$rendered_compose" || die "Rendered Compose lost the private staging edge binding."
grep -Fq 'published: "18080"' "$rendered_compose" || die "Rendered Compose lost the staging edge port."
grep -Fq 'subnet: 172.31.0.0/24' "$rendered_compose" || die "Rendered Compose lost the staging proxy subnet."
grep -Fq 'subnet: 172.31.1.0/24' "$rendered_compose" || die "Rendered Compose lost the staging backend subnet."
! grep -Eq 'resume-screening-v3_(postgres_data|uploads_data|caddy_data|caddy_config|proxy|backend)' "$rendered_compose" || die "Rendered Compose references a production resource."
! grep -Eq 'published: "(80|443)"' "$rendered_compose" || die "Rendered Compose publishes a production public port."

previous_record="$history_dir/current-release.env"
api_image="greatsellai-hr-api:$release_commit"
caddy_image="greatsellai-hr-caddy:$release_commit"
api_image_id="$(require_image "$api_image" "")"
caddy_image_id="$(require_image "$caddy_image" "")"

# This is the only mutable application file in the staging project directory.
# It is sourced from the immutable stg tag, never from production source.
sudo -n install -m 600 "$candidate_compose" "$project_dir/compose.yml"
sudo -n env "RESUME_V3_RELEASE_IMAGE_TAG=$release_commit" docker compose \
  --project-directory "$project_dir" \
  -f "$project_dir/compose.yml" \
  --env-file "$project_dir/.env.staging" \
  up -d --no-build --remove-orphans

api_container="$(sudo -n env "RESUME_V3_RELEASE_IMAGE_TAG=$release_commit" docker compose \
  --project-directory "$project_dir" -f "$project_dir/compose.yml" \
  --env-file "$project_dir/.env.staging" ps -q api)"
caddy_container="$(sudo -n env "RESUME_V3_RELEASE_IMAGE_TAG=$release_commit" docker compose \
  --project-directory "$project_dir" -f "$project_dir/compose.yml" \
  --env-file "$project_dir/.env.staging" ps -q caddy)"
[[ -n "$api_container" && -n "$caddy_container" ]] || die "Staging API or Caddy container is missing after deployment."
[[ "$(sudo -n docker inspect --format '{{.Image}}' "$api_container")" == "$api_image_id" ]] || die "Staging API container image differs from the verified CI image."
[[ "$(sudo -n docker inspect --format '{{.Image}}' "$caddy_container")" == "$caddy_image_id" ]] || die "Staging Caddy container image differs from the verified CI image."

for attempt in $(seq 1 30); do
  if curl --fail --silent --show-error --connect-timeout 3 --max-time 8 \
    http://172.17.0.1:18080/health >/dev/null; then
    break
  fi
  [[ "$attempt" -eq 30 ]] && die "Staging private edge health check did not become ready."
  sleep 2
done

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
temporary_record="$history_dir/.current-staging-release.$$.tmp"
cat > "$temporary_record" <<EOF_RECORD
state=deployed
tag=$tag
commit=$release_commit
archive_sha256=$archive_sha256
ci_image_archive_sha256=$ci_image_archive_sha256
api_image_config_digest=$api_image_config_digest
caddy_image_config_digest=$caddy_image_config_digest
api_image_id=$api_image_id
caddy_image_id=$caddy_image_id
private_api_health_check=pass
private_edge_health_check=pass
public_smoke_check=pending
deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF_RECORD
mv -f "$temporary_record" "$previous_record"
printf 'Staging runtime deployed: %s (%s)\n' "$tag" "$release_commit"
EOF
)"

git show "$tag:deploy/compose.staging.yml" | ssh "${ssh_options[@]}" "$remote_host" \
  "bash -c $(shell_quote "$remote_deploy_script") -- $(shell_quote "$project_dir") $(shell_quote "$history_dir") $(shell_quote "$tag") $(shell_quote "$release_commit") $(shell_quote "$archive_sha256") $(shell_quote "$ci_image_archive_sha256") $(shell_quote "$api_image_config_digest") $(shell_quote "$caddy_image_config_digest")"

"$repo_root/scripts/smoke-test-staging.sh" "$public_url"

remote_mark_smoke_script="$(cat <<'EOF'
set -Eeuo pipefail
history_dir="$1"
tag="$2"
release_commit="$3"
record="$history_dir/current-release.env"
[[ "$history_dir" == /home/ubuntu/* && "$history_dir" == *staging* && "$history_dir" != /home/ubuntu/ ]]
[[ "$tag" =~ ^stg-[0-9]{8}-[0-9a-f]{7,40}$ ]]
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]]
test -f "$record"
record_value() { sed -n "s/^$2=//p" "$1" | tail -n 1; }
[[ "$(record_value "$record" state)" == "deployed" ]]
[[ "$(record_value "$record" tag)" == "$tag" ]]
[[ "$(record_value "$record" commit)" == "$release_commit" ]]
temporary_record="$history_dir/.current-staging-smoke.$$.tmp"
sed -e 's/^state=.*/state=complete/' -e 's/^public_smoke_check=.*/public_smoke_check=pass/' "$record" > "$temporary_record"
printf 'public_smoke_checked_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$temporary_record"
mv -f "$temporary_record" "$record"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
cp "$record" "$history_dir/releases/$timestamp-$tag.env"
EOF
)"

ssh "${ssh_options[@]}" "$remote_host" \
  "bash -c $(shell_quote "$remote_mark_smoke_script") -- $(shell_quote "$history_dir") $(shell_quote "$tag") $(shell_quote "$release_commit")"

echo "Staging deployment and public smoke checks succeeded: $tag ($release_commit)"
