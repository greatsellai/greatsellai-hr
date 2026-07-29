#!/usr/bin/env bash
# Public, non-destructive smoke checks for the isolated staging origin.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/smoke-test-staging.sh <https://staging-host>

Checks health, anonymous session protection, original-file protection, and the
canonical login page. Production and non-HTTPS URLs are refused.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

base_url="${1:-}"
[[ -n "$base_url" && "$base_url" != -* ]] || { usage >&2; exit 1; }
shift || true
[[ $# -eq 0 ]] || { usage >&2; exit 1; }
[[ "$base_url" =~ ^https://staging\.hr\.greatsellai\.net/?$ ]] || \
  die "Staging smoke checks only accept https://staging.hr.greatsellai.net."
base_url="${base_url%/}"

curl_common=(--fail --silent --show-error --connect-timeout 8 --max-time 20)

health_body="$(curl "${curl_common[@]}" "$base_url/health")"
[[ "$health_body" == *'"status":"ok"'* ]] || die "Unexpected staging health response."

session_body="$(curl "${curl_common[@]}" "$base_url/v1/auth/session")"
[[ "$session_body" == *'"authenticated":false'*'"login_required":true'* ]] || \
  die "Unexpected unauthenticated session response."

protected_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --connect-timeout 8 --max-time 20 \
  "$base_url/v1/resumes/00000000-0000-0000-0000-000000000000/original-file")"
[[ "$protected_status" == "401" ]] || die "Original-file endpoint did not reject anonymous access."

login_body="$(curl "${curl_common[@]}" "$base_url/login")"
[[ "$login_body" == *'id="root"'* ]] || die "Staging login page did not render the application shell."

echo "Staging public smoke checks passed for $base_url."
