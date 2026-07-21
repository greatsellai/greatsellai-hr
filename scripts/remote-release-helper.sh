#!/usr/bin/env bash
# Runs on the deployment target. It is copied from the requested Git tag to a
# temporary server path, so Docker commands never consume an SSH script stream.
set -Eeuo pipefail

normalize_optional() {
  [[ "$1" == "__none__" ]] && printf '' || printf '%s' "$1"
}

precheck() {
  local project_dir="$1" history_dir="$2" tag="$3" release_commit="$4"
  local previous_tag previous_commit mode="$7" backup_required="$8"
  previous_tag="$(normalize_optional "$5")"
  previous_commit="$(normalize_optional "$6")"

  [[ -f "$project_dir/compose.yml" ]] || { echo "Live compose.yml is missing." >&2; exit 1; }
  [[ -f "$project_dir/.env.production" ]] || { echo "Live .env.production is missing." >&2; exit 1; }
  mkdir -p "$history_dir/releases" "$history_dir/backups"
  chmod 700 "$history_dir" "$history_dir/releases" "$history_dir/backups"

  if [[ "$backup_required" == "1" ]]; then
    local timestamp backup_path
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup_path="$history_dir/backups/pre-${tag}-${timestamp}.sql.gz"
    umask 077
    sudo -n docker compose --project-directory "$project_dir" -f "$project_dir/compose.yml" \
      --env-file "$project_dir/.env.production" exec -T db \
      pg_dump -U resume_v3 -d resume_v3 </dev/null | gzip > "$backup_path"
    [[ -s "$backup_path" ]] || { echo "Database backup is empty." >&2; exit 1; }
  fi

  umask 077
  cat > "$history_dir/pending-release.env" <<EOF
tag=$tag
commit=$release_commit
previous_tag=$previous_tag
previous_commit=$previous_commit
mode=$mode
backup_required=$backup_required
prepared_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
}

deploy() {
  local project_dir="$1" history_dir="$2" tag="$3" release_commit="$4"
  local previous_tag previous_commit mode="$7" skip_migrate="$8"
  previous_tag="$(normalize_optional "$5")"
  previous_commit="$(normalize_optional "$6")"
  local -a compose=(sudo -n docker compose --project-directory "$project_dir" -f "$project_dir/compose.yml" --env-file "$project_dir/.env.production")

  if [[ "$skip_migrate" == "1" ]]; then
    "${compose[@]}" up --build -d --no-deps api worker caddy </dev/null
  else
    "${compose[@]}" up --build -d api worker caddy </dev/null
  fi
  "${compose[@]}" exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile </dev/null >/dev/null

  local domain_addresses health_domain session_body protected_status timestamp record schema_action
  domain_addresses="$(sed -n 's/^RESUME_V3_DOMAIN=//p' "$project_dir/.env.production" | tail -n 1 | tr -d '\r' | sed -e 's/^"//' -e 's/"$//')"
  [[ -n "$domain_addresses" ]] || { echo "RESUME_V3_DOMAIN is not set." >&2; exit 1; }
  # Caddy accepts a comma-separated site-address list (for example, an apex
  # domain plus www). The first address remains the canonical endpoint used by
  # release health and authentication checks.
  health_domain="${domain_addresses%%,*}"
  health_domain="$(printf '%s' "$health_domain" | xargs)"
  [[ -n "$health_domain" ]] || { echo "RESUME_V3_DOMAIN has no primary address." >&2; exit 1; }
  for attempt in $(seq 1 30); do
    if curl --fail --silent --show-error --connect-timeout 5 --max-time 15 "https://$health_domain/health" >/dev/null; then
      break
    fi
    [[ "$attempt" -eq 30 ]] && { echo "HTTPS health check did not become ready." >&2; exit 1; }
    sleep 2
  done
  session_body="$(curl --fail --silent --show-error --connect-timeout 5 --max-time 15 "https://$health_domain/v1/auth/session")"
  [[ "$session_body" == *'"authenticated":false'*'"login_required":true'* ]] || { echo "Unexpected unauthenticated session response." >&2; exit 1; }
  protected_status="$(curl --silent --output /dev/null --write-out '%{http_code}' --connect-timeout 5 --max-time 15 "https://$health_domain/v1/resumes/00000000-0000-0000-0000-000000000000/original-file")"
  [[ "$protected_status" == "401" ]] || { echo "Protected PDF endpoint did not reject an unauthenticated request." >&2; exit 1; }

  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  record="$history_dir/releases/${timestamp}-${tag}.env"
  schema_action="migrate_checked"
  [[ "$skip_migrate" == "1" ]] && schema_action="preserved"
  umask 077
  cat > "$record" <<EOF
tag=$tag
commit=$release_commit
previous_tag=$previous_tag
previous_commit=$previous_commit
mode=$mode
database_schema_action=$schema_action
health_check=pass
session_protection=pass
protected_pdf_check=pass
deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  cp "$record" "$history_dir/current-release.env"
  rm -f "$history_dir/pending-release.env"
  printf 'Deployment recorded: %s\n' "$record"
}

case "${1:-}" in
  precheck) shift; precheck "$@" ;;
  deploy) shift; deploy "$@" ;;
  *) echo "Usage: $0 {precheck|deploy} <release arguments>" >&2; exit 2 ;;
esac
