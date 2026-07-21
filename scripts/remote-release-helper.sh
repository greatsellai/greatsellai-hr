#!/usr/bin/env bash
# Runs on the deployment target. It is copied from a reviewed Git tag to a
# temporary server path, so Docker commands never consume an SSH script stream.
# The helper never reads or prints production secrets.
set -Eeuo pipefail

readonly uploads_volume_name="resume-screening-v3_uploads_data"

normalize_optional() {
  [[ "$1" == "__none__" ]] && printf '' || printf '%s' "$1"
}

compose_run() {
  local project_dir="$1" release_commit="$2"
  shift 2
  sudo -n env "RESUME_V3_RELEASE_IMAGE_TAG=$release_commit" \
    docker compose --project-directory "$project_dir" -f "$project_dir/compose.yml" \
    --env-file "$project_dir/.env.production" "$@"
}

record_value() {
  local file="$1" key="$2"
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}

require_safe_backup_id() {
  local backup_id="$1"
  [[ "$backup_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$ ]] || {
    echo "Invalid backup ID." >&2
    exit 1
  }
}

uploads_volume_exists() {
  sudo -n docker volume inspect "$uploads_volume_name" >/dev/null 2>&1
}

validate_upload_archive() {
  local backup_dir="$1"
  sudo -n docker run --rm --network none --user 0 \
    -v "$backup_dir:/backup:ro" postgres:16-alpine \
    sh -ceu '
      tar -tzf /backup/uploads.tar.gz | while IFS= read -r entry; do
        case "$entry" in
          /*|..|../*|*/../*)
            echo "Unsafe path in uploads archive." >&2
            exit 1
            ;;
        esac
      done
    '
}

create_backup_bundle() (
  # One deployment backup must contain database metadata and the exact shared
  # originals volume under a single ID. API and worker are briefly quiesced so
  # mailbox ingestion cannot create a database/file split during the snapshot.
  set -Eeuo pipefail
  local project_dir="$1" history_dir="$2" tag="$3" release_commit="$4"
  local previous_tag="$5" previous_commit="$6" mode="$7"
  local db_container backup_id timestamp staging_dir final_dir
  local services_stopped=0 completed=0

  cleanup() {
    local status=$?
    if [[ "$services_stopped" == "1" ]]; then
      compose_run "$project_dir" "$release_commit" up -d --no-deps api worker >/dev/null 2>&1 || true
    fi
    if [[ "$completed" != "1" && -n "${staging_dir:-}" ]]; then
      rm -rf -- "$staging_dir"
    fi
    exit "$status"
  }
  trap cleanup EXIT

  db_container="$(compose_run "$project_dir" "$release_commit" ps -q db)"
  if [[ -z "$db_container" ]]; then
    if uploads_volume_exists; then
      echo "Refusing backup: uploads volume exists but PostgreSQL service is absent." >&2
      exit 1
    fi
    # A first deployment has no state to back up. It is recorded as an empty
    # initial state, never as a successful data backup.
    completed=1
    printf 'initial-empty\n'
    exit 0
  fi
  if ! uploads_volume_exists; then
    echo "Refusing backup: PostgreSQL service exists but uploads volume is absent." >&2
    exit 1
  fi

  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_id="pre-${tag}-${timestamp}"
  require_safe_backup_id "$backup_id"
  final_dir="$history_dir/backups/$backup_id"
  staging_dir="$history_dir/backups/.${backup_id}.partial"
  [[ ! -e "$final_dir" && ! -e "$staging_dir" ]] || {
    echo "Backup ID collision; retry the release." >&2
    exit 1
  }
  umask 077
  mkdir -p "$staging_dir"
  chmod 700 "$staging_dir"

  compose_run "$project_dir" "$release_commit" stop api worker >/dev/null
  services_stopped=1

  compose_run "$project_dir" "$release_commit" exec -T db \
    pg_dump -U resume_v3 -d resume_v3 -Fc </dev/null > "$staging_dir/database.dump"
  [[ -s "$staging_dir/database.dump" ]] || {
    echo "Database backup is empty." >&2
    exit 1
  }
  sudo -n docker run --rm --network none -v "$staging_dir:/backup:ro" postgres:16-alpine \
    pg_restore --list /backup/database.dump >/dev/null

  sudo -n docker run --rm --network none --user 0 \
    -v "$uploads_volume_name:/source:ro" -v "$staging_dir:/backup" postgres:16-alpine \
    sh -ceu 'tar -C /source -czf /backup/uploads.tar.gz .'
  [[ -s "$staging_dir/uploads.tar.gz" ]] || {
    echo "Uploads backup is empty." >&2
    exit 1
  }
  validate_upload_archive "$staging_dir"

  (
    cd "$staging_dir"
    sha256sum database.dump uploads.tar.gz > checksums.sha256
    sha256sum --check checksums.sha256 >/dev/null
  )
  cat > "$staging_dir/manifest.env" <<EOF
format_version=1
state=complete
backup_id=$backup_id
release_tag=$tag
release_commit=$release_commit
previous_tag=$previous_tag
previous_commit=$previous_commit
mode=$mode
database_file=database.dump
uploads_file=uploads.tar.gz
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

  # Do not publish a successful backup unless the temporarily quiesced runtime
  # has been brought back. A failed restart cleans the partial directory and
  # leaves the deployment caller with a hard failure instead of a false record.
  compose_run "$project_dir" "$release_commit" up -d --no-deps api worker >/dev/null
  services_stopped=0
  mv "$staging_dir" "$final_dir"
  completed=1
  printf '%s\n' "$backup_id"
)

precheck() {
  local project_dir="$1" history_dir="$2" tag="$3" release_commit="$4"
  local previous_tag previous_commit mode="$7" backup_required="$8"
  local backup_id="" backup_state="not_requested"
  previous_tag="$(normalize_optional "$5")"
  previous_commit="$(normalize_optional "$6")"

  [[ -f "$project_dir/compose.yml" ]] || { echo "Live compose.yml is missing." >&2; exit 1; }
  [[ -f "$project_dir/.env.production" ]] || { echo "Live .env.production is missing." >&2; exit 1; }
  mkdir -p "$history_dir/releases" "$history_dir/backups"
  chmod 700 "$history_dir" "$history_dir/releases" "$history_dir/backups"

  if [[ "$backup_required" == "1" ]]; then
    backup_id="$(create_backup_bundle "$project_dir" "$history_dir" "$tag" "$release_commit" "$previous_tag" "$previous_commit" "$mode")"
    if [[ "$backup_id" == "initial-empty" ]]; then
      backup_state="initial_empty"
      backup_id=""
    else
      backup_state="complete"
    fi
  fi

  umask 077
  cat > "$history_dir/pending-release.env" <<EOF
tag=$tag
commit=$release_commit
previous_tag=$previous_tag
previous_commit=$previous_commit
mode=$mode
backup_required=$backup_required
backup_state=$backup_state
backup_id=$backup_id
prepared_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
}

deploy() {
  local project_dir="$1" history_dir="$2" tag="$3" release_commit="$4"
  local previous_tag previous_commit mode="$7" skip_migrate="$8"
  local backup_id backup_state api_image_id caddy_image_id
  previous_tag="$(normalize_optional "$5")"
  previous_commit="$(normalize_optional "$6")"
  backup_id="$(record_value "$history_dir/pending-release.env" backup_id)"
  backup_state="$(record_value "$history_dir/pending-release.env" backup_state)"

  if [[ "$skip_migrate" == "1" ]]; then
    compose_run "$project_dir" "$release_commit" up --build -d --no-deps api worker caddy </dev/null
  else
    compose_run "$project_dir" "$release_commit" up --build -d api worker caddy </dev/null
  fi
  compose_run "$project_dir" "$release_commit" exec -T caddy \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile </dev/null >/dev/null

  local domain session_body protected_status timestamp record schema_action
  domain="$(sed -n 's/^RESUME_V3_DOMAIN=//p' "$project_dir/.env.production" | tail -n 1 | tr -d '\r' | sed -e 's/^"//' -e 's/"$//')"
  [[ -n "$domain" ]] || { echo "RESUME_V3_DOMAIN is not set." >&2; exit 1; }
  for attempt in $(seq 1 30); do
    if curl --fail --silent --show-error --connect-timeout 5 --max-time 15 "https://$domain/health" >/dev/null; then
      break
    fi
    [[ "$attempt" -eq 30 ]] && { echo "HTTPS health check did not become ready." >&2; exit 1; }
    sleep 2
  done
  session_body="$(curl --fail --silent --show-error --connect-timeout 5 --max-time 15 "https://$domain/v1/auth/session")"
  [[ "$session_body" == *'"authenticated":false'*'"login_required":true'* ]] || { echo "Unexpected unauthenticated session response." >&2; exit 1; }
  protected_status="$(curl --silent --output /dev/null --write-out '%{http_code}' --connect-timeout 5 --max-time 15 "https://$domain/v1/resumes/00000000-0000-0000-0000-000000000000/original-file")"
  [[ "$protected_status" == "401" ]] || { echo "Protected PDF endpoint did not reject an unauthenticated request." >&2; exit 1; }

  api_image_id="$(sudo -n docker image inspect --format '{{.Id}}' "greatsellai-hr-api:$release_commit")"
  caddy_image_id="$(sudo -n docker image inspect --format '{{.Id}}' "greatsellai-hr-caddy:$release_commit")"
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
backup_state=$backup_state
backup_id=$backup_id
api_image_id=$api_image_id
caddy_image_id=$caddy_image_id
health_check=pass
session_protection=pass
protected_pdf_check=pass
deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  cp "$record" "$history_dir/current-release.env"
  rm -f "$history_dir/pending-release.env"
  printf 'Deployment recorded: %s\n' "$record"
}

restore() {
  local project_dir="$1" history_dir="$2" backup_id="$3" confirmation="$4"
  local backup_dir manifest state manifest_id database_file uploads_file
  local current_tag current_commit safety_backup_id restore_started=0 services_stopped=0 success=0
  require_safe_backup_id "$backup_id"
  [[ "$confirmation" == "RESTORE" ]] || {
    echo "Restore requires the exact confirmation RESTORE." >&2
    exit 1
  }
  backup_dir="$history_dir/backups/$backup_id"
  manifest="$backup_dir/manifest.env"
  [[ -f "$manifest" ]] || { echo "Backup manifest is missing." >&2; exit 1; }
  state="$(record_value "$manifest" state)"
  manifest_id="$(record_value "$manifest" backup_id)"
  database_file="$(record_value "$manifest" database_file)"
  uploads_file="$(record_value "$manifest" uploads_file)"
  [[ "$state" == "complete" && "$manifest_id" == "$backup_id" ]] || {
    echo "Backup is not a complete, matching release backup." >&2
    exit 1
  }
  [[ "$database_file" == "database.dump" && "$uploads_file" == "uploads.tar.gz" ]] || {
    echo "Backup manifest has unexpected artifact names." >&2
    exit 1
  }
  [[ -f "$history_dir/current-release.env" ]] || {
    echo "Current release record is missing; refusing destructive restore." >&2
    exit 1
  }
  current_tag="$(record_value "$history_dir/current-release.env" tag)"
  current_commit="$(record_value "$history_dir/current-release.env" commit)"
  [[ "$current_tag" =~ ^prod-[0-9]{8}-[0-9a-f]{7,40}$ && "$current_commit" =~ ^[0-9a-f]{40}$ ]] || {
    echo "Current release record is missing; refusing destructive restore." >&2
    exit 1
  }
  (
    cd "$backup_dir"
    sha256sum --check checksums.sha256 >/dev/null
  )
  validate_upload_archive "$backup_dir"
  sudo -n docker run --rm --network none -v "$backup_dir:/backup:ro" postgres:16-alpine \
    pg_restore --list /backup/database.dump >/dev/null
  uploads_volume_exists || { echo "Uploads volume is missing." >&2; exit 1; }
  [[ -n "$(compose_run "$project_dir" "$current_commit" ps -q db)" ]] || {
    echo "PostgreSQL service is not running." >&2
    exit 1
  }

  # A restore always captures the current state first. This creates a coherent
  # escape hatch if an operator selected the wrong historical backup.
  safety_backup_id="$(create_backup_bundle "$project_dir" "$history_dir" "$current_tag" "$current_commit" "$current_tag" "$current_commit" "pre_restore")"
  [[ "$safety_backup_id" != "initial-empty" ]] || {
    echo "Refusing restore because the current state could not be backed up." >&2
    exit 1
  }

  cleanup_restore() {
    local status=$?
    if [[ "$services_stopped" == "1" && "$restore_started" == "0" ]]; then
      compose_run "$project_dir" "$current_commit" up -d --no-deps api worker >/dev/null 2>&1 || true
    elif [[ "$services_stopped" == "1" && "$success" != "1" ]]; then
      echo "Restore failed after data mutation; API and worker remain stopped. Use safety backup $safety_backup_id only after reviewing the failure." >&2
    fi
    exit "$status"
  }
  trap cleanup_restore EXIT

  compose_run "$project_dir" "$current_commit" stop api worker >/dev/null
  services_stopped=1
  restore_started=1
  compose_run "$project_dir" "$current_commit" exec -T db \
    pg_restore -U resume_v3 -d resume_v3 --clean --if-exists --no-owner < "$backup_dir/database.dump"
  sudo -n docker run --rm --network none --user 0 \
    -v "$uploads_volume_name:/target" -v "$backup_dir:/backup:ro" postgres:16-alpine \
    sh -ceu 'find /target -mindepth 1 -depth -exec rm -rf -- {} \; && tar -xzf /backup/uploads.tar.gz -C /target'
  compose_run "$project_dir" "$current_commit" up -d --no-deps api worker >/dev/null
  services_stopped=0
  success=1

  umask 077
  cat > "$history_dir/releases/restore-$(date -u +%Y%m%dT%H%M%SZ)-${backup_id}.env" <<EOF
restored_backup_id=$backup_id
safety_backup_id=$safety_backup_id
restored_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  printf 'Restore completed from %s (pre-restore safety backup: %s)\n' "$backup_id" "$safety_backup_id"
}

case "${1:-}" in
  precheck) shift; precheck "$@" ;;
  deploy) shift; deploy "$@" ;;
  restore) shift; restore "$@" ;;
  *) echo "Usage: $0 {precheck|deploy|restore} <release arguments>" >&2; exit 2 ;;
esac
