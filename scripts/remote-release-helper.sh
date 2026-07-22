#!/usr/bin/env bash
# Runs on the deployment target. Production environment files and candidate data
# remain on this host; a reviewed Git archive is staged into an immutable source
# directory before Docker is allowed to use it.
set -Eeuo pipefail

readonly uploads_volume_name="resume-screening-v3_uploads_data"
readonly postgres_volume_name="resume-screening-v3_postgres_data"
readonly release_sources_directory_name="release-sources"

current_tag=""
current_commit=""
current_source_dir=""

die() {
  echo "$*" >&2
  exit 1
}

normalize_optional() {
  [[ "$1" == "__none__" ]] && printf '' || printf '%s' "$1"
}

record_value() {
  sed -n "s/^$2=//p" "$1" | tail -n 1
}

require_safe_backup_id() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$ ]] || die "Invalid backup ID."
}

require_release_reference() {
  [[ "$1" =~ ^prod-[0-9]{8}-[0-9a-f]{7,40}$ ]] || die "Invalid production tag."
  [[ "$2" =~ ^[0-9a-f]{40}$ ]] || die "Invalid production commit."
}

release_sources_root() {
  printf '%s/%s' "$1" "$release_sources_directory_name"
}

validate_environment_dir() {
  [[ "$1" == /home/ubuntu/* && "$1" != /home/ubuntu/ ]] || die "Unsafe environment directory."
  [[ -f "$1/.env.production" ]] || die "Live .env.production is missing."
}

validate_history_dir() {
  [[ "$1" == /home/ubuntu/* && "$1" != /home/ubuntu/ ]] || die "Unsafe release history directory."
}

validate_source_dir() {
  local source_dir="$1" environment_dir="$2" history_dir="$3"
  local source_root
  source_root="$(release_sources_root "$history_dir")"
  [[ "$source_dir" == "$environment_dir" || "$source_dir" == "$source_root"/* ]] || \
    die "Release source directory is outside approved locations."
  [[ -f "$source_dir/compose.yml" ]] || die "Release source Compose file is missing."
  [[ -f "$source_dir/Dockerfile" ]] || die "Release source Dockerfile is missing."
  [[ -f "$source_dir/deploy/Caddy.Dockerfile" ]] || die "Release source Caddy Dockerfile is missing."
}

compose_run() {
  local source_dir="$1" environment_dir="$2" image_tag="$3"
  shift 3
  sudo -n env "RESUME_V3_RELEASE_IMAGE_TAG=$image_tag" \
    docker compose --project-directory "$source_dir" -f "$source_dir/compose.yml" \
      --env-file "$environment_dir/.env.production" "$@"
}

uploads_volume_exists() {
  sudo -n docker volume inspect "$uploads_volume_name" >/dev/null 2>&1
}

postgres_volume_exists() {
  sudo -n docker volume inspect "$postgres_volume_name" >/dev/null 2>&1
}

validate_upload_archive() {
  local backup_dir="$1"
  sudo -n docker run --rm --network none --user 0 \
    -v "$backup_dir:/backup:ro" postgres:16-alpine \
    sh -ceu '
      # Validate the gzip/tar stream before inspecting member names. A plain
      # pipeline into ``while`` would otherwise mask a failed tar reader when
      # the shell lacks pipefail.
      tar -tzf /backup/uploads.tar.gz >/dev/null

      # Release backups may contain directories and regular uploaded files,
      # never links, devices, FIFOs, or other special members.
      tar -tvzf /backup/uploads.tar.gz | while IFS= read -r listing; do
        case "$listing" in
          -*|d*) ;;
          *)
            echo "Unsupported uploads archive member type." >&2
            exit 1
            ;;
        esac
      done

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

load_current_runtime() {
  local environment_dir="$1" history_dir="$2"
  local record
  current_tag=""
  current_commit=""
  current_source_dir="$environment_dir"
  record="$history_dir/current-release.env"
  [[ -f "$record" ]] || return 0

  current_tag="$(record_value "$record" tag)"
  current_commit="$(record_value "$record" commit)"
  current_source_dir="$(record_value "$record" source_dir)"
  [[ -n "$current_source_dir" ]] || current_source_dir="$environment_dir"
  require_release_reference "$current_tag" "$current_commit"
  validate_source_dir "$current_source_dir" "$environment_dir" "$history_dir"
}

resolve_pending_release() {
  local history_dir="$1" pending_record
  local pending_tag pending_commit pending_backup_id
  pending_record="$history_dir/pending-release.env"
  [[ -f "$pending_record" ]] || return 0

  pending_tag="$(record_value "$pending_record" tag)"
  pending_commit="$(record_value "$pending_record" commit)"
  pending_backup_id="$(record_value "$pending_record" backup_id)"
  require_release_reference "$pending_tag" "$pending_commit"

  die "Unresolved pending release for $pending_tag ($pending_commit), backup ${pending_backup_id:-none}. Use an explicit, audited reconciliation after verifying a paired backup; refusing to overwrite interrupted release state."
}

create_backup_bundle() (
  # Always operate the *currently recorded* source/image. The target image may
  # not exist yet; using it here would make the failure recovery path unsafe.
  set -Eeuo pipefail
  local runtime_source_dir="$1" environment_dir="$2" history_dir="$3"
  local target_tag="$4" target_commit="$5" previous_tag="$6" previous_commit="$7" mode="$8"
  local backup_id="" timestamp="" staging_dir="" final_dir=""
  local services_stopped=0 completed=0

  cleanup() {
    local status=$?
    if [[ "$services_stopped" == "1" ]]; then
      compose_run "$runtime_source_dir" "$environment_dir" "$previous_commit" \
        up -d --no-build --no-deps api worker >/dev/null 2>&1 || true
    fi
    if [[ "$completed" != "1" && -n "$staging_dir" ]]; then
      rm -rf -- "$staging_dir"
    fi
    exit "$status"
  }
  trap cleanup EXIT

  if [[ -z "$previous_commit" ]]; then
    completed=1
    printf 'initial-empty\n'
    exit 0
  fi

  if [[ -z "$(compose_run "$runtime_source_dir" "$environment_dir" "$previous_commit" ps -q db)" ]]; then
    if uploads_volume_exists || postgres_volume_exists; then
      die "Refusing backup: persisted data volume exists but PostgreSQL service is absent."
    fi
    completed=1
    printf 'initial-empty\n'
    exit 0
  fi
  uploads_volume_exists || die "Refusing backup: PostgreSQL service exists but uploads volume is absent."
  postgres_volume_exists || die "Refusing backup: PostgreSQL service exists but PostgreSQL volume is absent."

  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_id="pre-$target_tag-$timestamp"
  require_safe_backup_id "$backup_id"
  final_dir="$history_dir/backups/$backup_id"
  staging_dir="$history_dir/backups/.$backup_id.partial"
  [[ ! -e "$final_dir" && ! -e "$staging_dir" ]] || die "Backup ID collision; retry the release."
  umask 077
  mkdir -p "$staging_dir"
  chmod 700 "$staging_dir"

  compose_run "$runtime_source_dir" "$environment_dir" "$previous_commit" stop api worker >/dev/null
  services_stopped=1

  compose_run "$runtime_source_dir" "$environment_dir" "$previous_commit" exec -T db \
    pg_dump -U resume_v3 -d resume_v3 -Fc </dev/null > "$staging_dir/database.dump"
  [[ -s "$staging_dir/database.dump" ]] || die "Database backup is empty."
  sudo -n docker run --rm --network none -v "$staging_dir:/backup:ro" postgres:16-alpine \
    pg_restore --list /backup/database.dump >/dev/null

  sudo -n docker run --rm --network none --user 0 \
    -v "$uploads_volume_name:/source:ro" -v "$staging_dir:/backup" postgres:16-alpine \
    sh -ceu 'tar -C /source -czf /backup/uploads.tar.gz .'
  [[ -s "$staging_dir/uploads.tar.gz" ]] || die "Uploads backup is empty."
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
release_tag=$target_tag
release_commit=$target_commit
previous_tag=$previous_tag
previous_commit=$previous_commit
mode=$mode
database_file=database.dump
uploads_file=uploads.tar.gz
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

  compose_run "$runtime_source_dir" "$environment_dir" "$previous_commit" \
    up -d --no-build --no-deps api worker >/dev/null
  services_stopped=0
  mv "$staging_dir" "$final_dir"
  completed=1
  printf '%s\n' "$backup_id"
)

stage_target_source() {
  local history_dir="$1" target_commit="$2" archive_sha256="$3" stage_tool="$4"
  local source_root
  source_root="$(release_sources_root "$history_dir")"
  [[ "$archive_sha256" =~ ^[0-9a-f]{64}$ ]] || die "Invalid release archive checksum."
  [[ -f "$stage_tool" ]] || die "Release source stager is missing."
  mkdir -p "$source_root"
  chmod 700 "$source_root"
  python3 "$stage_tool" --release-root "$source_root" --release-commit "$target_commit" \
    --archive-sha256 "$archive_sha256" >/dev/null
  printf '%s/%s' "$source_root" "$target_commit"
}

prepare_target_images() {
  local target_source_dir="$1" environment_dir="$2" history_dir="$3" target_commit="$4"
  validate_source_dir "$target_source_dir" "$environment_dir" "$history_dir"
  compose_run "$target_source_dir" "$environment_dir" "$target_commit" config --quiet
  compose_run "$target_source_dir" "$environment_dir" "$target_commit" build api caddy
  sudo -n docker image inspect "greatsellai-hr-api:$target_commit" >/dev/null
  sudo -n docker image inspect "greatsellai-hr-caddy:$target_commit" >/dev/null
}

write_release_records() {
  local history_dir="$1" tag="$2" target_commit="$3" target_source_dir="$4"
  local mode="$5" skip_migrate="$6" backup_state="$7" backup_id="$8"
  local timestamp record temporary_record temporary_current api_image_id caddy_image_id
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  record="$history_dir/releases/$timestamp-$tag.env"
  temporary_record="$history_dir/releases/.$timestamp-$tag.partial"
  temporary_current="$history_dir/.current-release.$$.tmp"
  api_image_id="$(sudo -n docker image inspect --format '{{.Id}}' "greatsellai-hr-api:$target_commit")"
  caddy_image_id="$(sudo -n docker image inspect --format '{{.Id}}' "greatsellai-hr-caddy:$target_commit")"
  umask 077
  cat > "$temporary_current" <<EOF
state=complete
tag=$tag
commit=$target_commit
source_dir=$target_source_dir
previous_tag=$current_tag
previous_commit=$current_commit
previous_source_dir=$current_source_dir
mode=$mode
database_schema_action=$([[ "$skip_migrate" == "1" ]] && printf preserved || printf migrate_checked)
backup_state=$backup_state
backup_id=$backup_id
api_image_id=$api_image_id
caddy_image_id=$caddy_image_id
health_check=pass
session_protection=pass
protected_pdf_check=pass
deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  # Commit the authoritative current record first. No release history file is
  # created before this move, so a failed record write cannot make a target
  # that was recovered to the old runtime look rollback-eligible.
  mv -f "$temporary_current" "$history_dir/current-release.env"
  # The current record is authoritative. Once it advances, the target runtime
  # has passed all health checks and must not be rolled back merely because a
  # later audit-file write hits an I/O error. Bash dynamic scope updates the
  # ``deployment_succeeded`` local owned by deploy_target.
  deployment_succeeded=1
  cp "$history_dir/current-release.env" "$temporary_record"
  mv "$temporary_record" "$record"
  printf 'Deployment recorded: %s\n' "$record"
}

deploy_target() {
  local environment_dir="$1" history_dir="$2" tag="$3" target_commit="$4" target_source_dir="$5"
  local mode="$6" migration_changed="$7" skip_migrate="$8" stage_tool="$9"
  local backup_id backup_state deployment_succeeded=0
  backup_id="$(record_value "$history_dir/pending-release.env" backup_id)"
  backup_state="$(record_value "$history_dir/pending-release.env" backup_state)"

  recover_previous_runtime() {
    local status=$?
    if [[ "$deployment_succeeded" != "1" && -n "$current_commit" && "$migration_changed" == "0" ]]; then
      compose_run "$current_source_dir" "$environment_dir" "$current_commit" \
        up -d --no-build --no-deps api worker caddy >/dev/null 2>&1 || true
    fi
    exit "$status"
  }
  trap recover_previous_runtime EXIT

  if [[ "$skip_migrate" == "1" ]]; then
    compose_run "$target_source_dir" "$environment_dir" "$target_commit" \
      up -d --no-build --no-deps api worker caddy </dev/null
  else
    compose_run "$target_source_dir" "$environment_dir" "$target_commit" \
      up -d --no-build api worker caddy </dev/null
  fi
  compose_run "$target_source_dir" "$environment_dir" "$target_commit" exec -T caddy \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile </dev/null >/dev/null

  local domain session_body protected_status
  domain="$(sed -n 's/^RESUME_V3_DOMAIN=//p' "$environment_dir/.env.production" | tail -n 1 | tr -d '\r' | sed -e 's/^"//' -e 's/"$//')"
  [[ -n "$domain" ]] || die "RESUME_V3_DOMAIN is not set."
  for attempt in $(seq 1 30); do
    if curl --fail --silent --show-error --connect-timeout 5 --max-time 15 "https://$domain/health" >/dev/null; then
      break
    fi
    [[ "$attempt" -eq 30 ]] && die "HTTPS health check did not become ready."
    sleep 2
  done
  session_body="$(curl --fail --silent --show-error --connect-timeout 5 --max-time 15 "https://$domain/v1/auth/session")"
  [[ "$session_body" == *'"authenticated":false'*'"login_required":true'* ]] || \
    die "Unexpected unauthenticated session response."
  protected_status="$(curl --silent --output /dev/null --write-out '%{http_code}' --connect-timeout 5 --max-time 15 "https://$domain/v1/resumes/00000000-0000-0000-0000-000000000000/original-file")"
  [[ "$protected_status" == "401" ]] || die "Protected PDF endpoint did not reject an unauthenticated request."

  write_release_records "$history_dir" "$tag" "$target_commit" "$target_source_dir" \
    "$mode" "$skip_migrate" "$backup_state" "$backup_id"
  # ``current-release.env`` is the source of truth for an active deployment.
  # The symlink is only an operator convenience, so it cannot undo a healthy
  # target if that optional update fails.
  if ! python3 "$stage_tool" --release-root "$(release_sources_root "$history_dir")" \
    --activate-source "$target_source_dir" >/dev/null; then
    echo "Warning: deployment is healthy but current-source could not be updated." >&2
  fi
  if ! rm -f "$history_dir/pending-release.env"; then
    echo "Warning: deployment is healthy but pending release metadata remains." >&2
  fi
  trap - EXIT
}

release_unlocked() {
  local environment_dir="$1" history_dir="$2" tag="$3" target_commit="$4"
  local expected_previous_tag="$5" expected_previous_commit="$6" mode="$7"
  local migration_changed="$8" skip_migrate="$9" archive_sha256="${10}" stage_tool="${11}"
  local target_source_dir backup_id backup_state

  validate_environment_dir "$environment_dir"
  validate_history_dir "$history_dir"
  require_release_reference "$tag" "$target_commit"
  [[ "$mode" == "deploy" || "$mode" == "rollback" ]] || die "Invalid release mode."
  [[ "$migration_changed" == "0" || "$migration_changed" == "1" ]] || die "Invalid migration flag."
  [[ "$skip_migrate" == "0" || "$skip_migrate" == "1" ]] || die "Invalid skip-migrate flag."
  mkdir -p "$history_dir/releases" "$history_dir/backups"
  chmod 700 "$history_dir" "$history_dir/releases" "$history_dir/backups"

  load_current_runtime "$environment_dir" "$history_dir"
  resolve_pending_release "$history_dir"
  target_source_dir="$(stage_target_source "$history_dir" "$target_commit" "$archive_sha256" "$stage_tool")"
  prepare_target_images "$target_source_dir" "$environment_dir" "$history_dir" "$target_commit"
  load_current_runtime "$environment_dir" "$history_dir"
  [[ "$current_tag" == "$(normalize_optional "$expected_previous_tag")" ]] || \
    die "Current release changed during preparation; retry the deployment."
  [[ "$current_commit" == "$(normalize_optional "$expected_previous_commit")" ]] || \
    die "Current release changed during preparation; retry the deployment."

  if [[ -z "$current_commit" ]]; then
    if [[ -n "$(compose_run "$target_source_dir" "$environment_dir" "$target_commit" ps -q db)" ]]; then
      die "Current release record is missing; refusing to stop a populated runtime."
    fi
    if uploads_volume_exists || postgres_volume_exists; then
      die "Current release record is missing; refusing to treat persistent data as an initial deployment."
    fi
    backup_id=""
    backup_state="initial_empty"
  else
    backup_id="$(create_backup_bundle "$current_source_dir" "$environment_dir" "$history_dir" \
      "$tag" "$target_commit" "$current_tag" "$current_commit" "$mode")"
    if [[ "$backup_id" == "initial-empty" ]]; then
      backup_id=""
      backup_state="initial_empty"
    else
      backup_state="complete"
    fi
  fi

  umask 077
  cat > "$history_dir/.pending-release.$$.tmp" <<EOF
tag=$tag
commit=$target_commit
source_dir=$target_source_dir
previous_tag=$current_tag
previous_commit=$current_commit
previous_source_dir=$current_source_dir
mode=$mode
backup_state=$backup_state
backup_id=$backup_id
prepared_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  mv -f "$history_dir/.pending-release.$$.tmp" "$history_dir/pending-release.env"
  deploy_target "$environment_dir" "$history_dir" "$tag" "$target_commit" "$target_source_dir" \
    "$mode" "$migration_changed" "$skip_migrate" "$stage_tool"
}

with_release_lock() {
  local history_dir="$1"
  shift
  validate_history_dir "$history_dir"
  mkdir -p "$history_dir"
  chmod 700 "$history_dir"
  command -v flock >/dev/null 2>&1 || die "flock is required for production releases."
  exec 9>"$history_dir/release.lock"
  flock -n 9 || {
    echo "Another production release, rollback, or restore is already running." >&2
    exit 75
  }
  "$@"
}

release() {
  local environment_dir="$1" history_dir="$2"
  shift 2
  with_release_lock "$history_dir" release_unlocked "$environment_dir" "$history_dir" "$@"
}

restore_unlocked() {
  local environment_dir="$1" history_dir="$2" backup_id="$3" confirmation="$4"
  local backup_dir manifest state manifest_id database_file uploads_file safety_backup_id
  local restore_started=0 services_stopped=0 success=0

  validate_environment_dir "$environment_dir"
  validate_history_dir "$history_dir"
  require_safe_backup_id "$backup_id"
  [[ "$confirmation" == "RESTORE" ]] || die "Restore requires the exact confirmation RESTORE."
  load_current_runtime "$environment_dir" "$history_dir"
  resolve_pending_release "$history_dir"
  [[ -n "$current_commit" ]] || die "Current release record is missing; refusing destructive restore."

  backup_dir="$history_dir/backups/$backup_id"
  manifest="$backup_dir/manifest.env"
  [[ -f "$manifest" ]] || die "Backup manifest is missing."
  state="$(record_value "$manifest" state)"
  manifest_id="$(record_value "$manifest" backup_id)"
  database_file="$(record_value "$manifest" database_file)"
  uploads_file="$(record_value "$manifest" uploads_file)"
  [[ "$state" == "complete" && "$manifest_id" == "$backup_id" ]] || \
    die "Backup is not a complete, matching release backup."
  [[ "$database_file" == "database.dump" && "$uploads_file" == "uploads.tar.gz" ]] || \
    die "Backup manifest has unexpected artifact names."
  (
    cd "$backup_dir"
    sha256sum --check checksums.sha256 >/dev/null
  )
  validate_upload_archive "$backup_dir"
  sudo -n docker run --rm --network none -v "$backup_dir:/backup:ro" postgres:16-alpine \
    pg_restore --list /backup/database.dump >/dev/null
  uploads_volume_exists || die "Uploads volume is missing."
  postgres_volume_exists || die "PostgreSQL volume is missing."
  [[ -n "$(compose_run "$current_source_dir" "$environment_dir" "$current_commit" ps -q db)" ]] || \
    die "PostgreSQL service is not running."

  safety_backup_id="$(create_backup_bundle "$current_source_dir" "$environment_dir" "$history_dir" \
    "$current_tag" "$current_commit" "$current_tag" "$current_commit" "pre_restore")"
  [[ "$safety_backup_id" != "initial-empty" ]] || \
    die "Refusing restore because the current state could not be backed up."

  cleanup_restore() {
    local status=$?
    if [[ "$services_stopped" == "1" && "$restore_started" == "0" ]]; then
      compose_run "$current_source_dir" "$environment_dir" "$current_commit" \
        up -d --no-build --no-deps api worker >/dev/null 2>&1 || true
    elif [[ "$services_stopped" == "1" && "$success" != "1" ]]; then
      echo "Restore failed after data mutation; API and worker remain stopped. Review safety backup $safety_backup_id." >&2
    fi
    exit "$status"
  }
  trap cleanup_restore EXIT

  compose_run "$current_source_dir" "$environment_dir" "$current_commit" stop api worker >/dev/null
  services_stopped=1
  restore_started=1
  compose_run "$current_source_dir" "$environment_dir" "$current_commit" exec -T db \
    pg_restore -U resume_v3 -d resume_v3 --clean --if-exists --no-owner < "$backup_dir/database.dump"
  sudo -n docker run --rm --network none --user 0 \
    -v "$uploads_volume_name:/target" -v "$backup_dir:/backup:ro" postgres:16-alpine \
    sh -ceu 'find /target -mindepth 1 -depth -exec rm -rf -- {} \; && tar -xzf /backup/uploads.tar.gz -C /target'
  compose_run "$current_source_dir" "$environment_dir" "$current_commit" \
    up -d --no-build --no-deps api worker >/dev/null
  services_stopped=0
  success=1

  umask 077
  cat > "$history_dir/releases/restore-$(date -u +%Y%m%dT%H%M%SZ)-$backup_id.env" <<EOF
restored_backup_id=$backup_id
safety_backup_id=$safety_backup_id
restored_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  printf 'Restore completed from %s (pre-restore safety backup: %s)\n' "$backup_id" "$safety_backup_id"
}

restore() {
  local environment_dir="$1" history_dir="$2"
  shift 2
  with_release_lock "$history_dir" restore_unlocked "$environment_dir" "$history_dir" "$@"
}

legacy_db_container() {
  local containers
  containers="$(sudo -n docker ps -q \
    --filter label=com.docker.compose.project=resume-screening-v3 \
    --filter label=com.docker.compose.service=db)"
  [[ -n "$containers" && "$(printf '%s\n' "$containers" | wc -l | tr -d ' ')" == "1" ]] || \
    die "Legacy reconciliation requires exactly one running PostgreSQL container."
  sudo -n docker inspect --format '{{.Name}}' "$containers" | sed 's#^/##'
}

require_legacy_runtime_quiescent() {
  local service
  for service in api worker caddy; do
    [[ -z "$(sudo -n docker ps -q \
      --filter label=com.docker.compose.project=resume-screening-v3 \
      --filter "label=com.docker.compose.service=$service")" ]] || \
      die "Legacy reconciliation refuses a runtime with $service still running."
  done
}

legacy_migrate_state() {
  local containers state
  containers="$(sudo -n docker ps -aq \
    --filter label=com.docker.compose.project=resume-screening-v3 \
    --filter label=com.docker.compose.service=migrate)"
  [[ -z "$containers" ]] && {
    printf absent
    return 0
  }
  [[ "$(printf '%s\n' "$containers" | wc -l | tr -d ' ')" == "1" ]] || \
    die "Legacy reconciliation refuses ambiguous migrate container history."
  state="$(sudo -n docker inspect --format '{{.State.Status}}:{{.State.ExitCode}}' "$containers")"
  [[ "$state" != running:* ]] || die "Legacy reconciliation refuses a running migration container."
  printf '%s' "$state"
}

require_legacy_compose_volume() {
  local volume="$1" expected_logical_name="$2"
  local project_label logical_label
  project_label="$(sudo -n docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "$volume")"
  logical_label="$(sudo -n docker volume inspect --format '{{index .Labels "com.docker.compose.volume"}}' "$volume")"
  [[ "$project_label" == "resume-screening-v3" && "$logical_label" == "$expected_logical_name" ]] || \
    die "Legacy reconciliation refuses a volume without the expected Compose ownership labels."
}

require_legacy_uploads_volume_provenance() {
  local project_label logical_label
  project_label="$(sudo -n docker volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "$uploads_volume_name")"
  logical_label="$(sudo -n docker volume inspect --format '{{index .Labels "com.docker.compose.volume"}}' "$uploads_volume_name")"
  if [[ "$project_label" == "resume-screening-v3" && "$logical_label" == "uploads_data" ]]; then
    return 0
  fi
  # Some legacy Compose versions created this named volume without labels. It
  # is still provably the active original-file volume only when both retained
  # API and worker containers mount this exact volume at the app data path.
  [[ -z "$project_label" && -z "$logical_label" ]] || \
    die "Legacy reconciliation refuses uploads volume ownership-label mismatch."
  require_legacy_container_volume_mount api "$uploads_volume_name" /var/lib/resume-v3/uploads
  require_legacy_container_volume_mount worker "$uploads_volume_name" /var/lib/resume-v3/uploads
}

require_legacy_container_volume_mount() {
  local service="$1" volume="$2" destination="$3"
  local containers matched
  containers="$(sudo -n docker ps -aq \
    --filter label=com.docker.compose.project=resume-screening-v3 \
    --filter "label=com.docker.compose.service=$service")"
  [[ -n "$containers" && "$(printf '%s\n' "$containers" | wc -l | tr -d ' ')" == "1" ]] || \
    die "Legacy reconciliation requires exactly one $service container for volume verification."
  matched="$(sudo -n docker inspect --format "{{range .Mounts}}{{if and (eq .Name \"$volume\") (eq .Destination \"$destination\")}}matched{{end}}{{end}}" "$containers")"
  [[ "$matched" == "matched" ]] || \
    die "Legacy reconciliation refuses a container without the expected persistent-volume mount."
}

create_legacy_reconciliation_backup() (
  # The interrupted legacy deploy left the source/image relationship
  # ambiguous. This snapshot intentionally avoids compose lifecycle commands:
  # every application writer is already confirmed stopped while PostgreSQL is
  # healthy, so a direct database + named-volume backup is deterministic.
  set -Eeuo pipefail
  local db_container="$1" history_dir="$2" pending_tag="$3" pending_commit="$4"
  local previous_tag="$5" previous_commit="$6"
  local timestamp backup_id staging_dir final_dir completed=0

  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_id="reconcile-$pending_tag-$timestamp"
  require_safe_backup_id "$backup_id"
  final_dir="$history_dir/backups/$backup_id"
  staging_dir="$history_dir/backups/.$backup_id.partial"
  [[ ! -e "$final_dir" && ! -e "$staging_dir" ]] || die "Legacy reconciliation backup ID collision; retry."

  cleanup() {
    local status=$?
    if [[ "$completed" != "1" && -n "$staging_dir" ]]; then
      rm -rf -- "$staging_dir"
    fi
    exit "$status"
  }
  trap cleanup EXIT

  umask 077
  mkdir -p "$staging_dir"
  chmod 700 "$staging_dir"
  sudo -n docker exec "$db_container" pg_isready -U resume_v3 -d resume_v3 >/dev/null
  sudo -n docker exec "$db_container" pg_dump -U resume_v3 -d resume_v3 -Fc </dev/null \
    > "$staging_dir/database.dump"
  [[ -s "$staging_dir/database.dump" ]] || die "Legacy reconciliation database backup is empty."
  sudo -n docker run --rm --network none -v "$staging_dir:/backup:ro" postgres:16-alpine \
    pg_restore --list /backup/database.dump >/dev/null

  sudo -n docker run --rm --network none --user 0 \
    -v "$uploads_volume_name:/source:ro" -v "$staging_dir:/backup" postgres:16-alpine \
    sh -ceu 'tar -C /source -czf /backup/uploads.tar.gz .'
  [[ -s "$staging_dir/uploads.tar.gz" ]] || die "Legacy reconciliation uploads backup is empty."
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
release_tag=$pending_tag
release_commit=$pending_commit
previous_tag=$previous_tag
previous_commit=$previous_commit
mode=legacy_reconcile
database_file=database.dump
uploads_file=uploads.tar.gz
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

  mv "$staging_dir" "$final_dir"
  completed=1
  printf '%s\n' "$backup_id"
)

reconcile_legacy_pending_unlocked() {
  local environment_dir="$1" history_dir="$2" expected_pending_tag="$3"
  local expected_pending_commit="$4" confirmation="$5"
  local pending_record current_record pending_tag pending_commit pending_previous_tag
  local pending_previous_commit pending_state current_tag current_commit current_state
  local db_container backup_id migrate_state timestamp archive temporary_archive

  validate_environment_dir "$environment_dir"
  validate_history_dir "$history_dir"
  require_release_reference "$expected_pending_tag" "$expected_pending_commit"
  [[ "$confirmation" == "RECONCILE_LEGACY_PENDING" ]] || \
    die "Legacy reconciliation requires the exact confirmation RECONCILE_LEGACY_PENDING."
  mkdir -p "$history_dir/releases" "$history_dir/backups"
  chmod 700 "$history_dir" "$history_dir/releases" "$history_dir/backups"

  pending_record="$history_dir/pending-release.env"
  current_record="$history_dir/current-release.env"
  [[ -f "$pending_record" ]] || die "Legacy reconciliation requires a pending release record."
  [[ -f "$current_record" ]] || die "Legacy reconciliation requires a current release record."

  pending_tag="$(record_value "$pending_record" tag)"
  pending_commit="$(record_value "$pending_record" commit)"
  pending_previous_tag="$(record_value "$pending_record" previous_tag)"
  pending_previous_commit="$(record_value "$pending_record" previous_commit)"
  pending_state="$(record_value "$pending_record" state)"
  current_tag="$(record_value "$current_record" tag)"
  current_commit="$(record_value "$current_record" commit)"
  current_state="$(record_value "$current_record" state)"

  require_release_reference "$pending_tag" "$pending_commit"
  require_release_reference "$pending_previous_tag" "$pending_previous_commit"
  require_release_reference "$current_tag" "$current_commit"
  [[ "$pending_tag" == "$expected_pending_tag" && "$pending_commit" == "$expected_pending_commit" ]] || \
    die "Pending release does not match the exact operator-confirmed legacy target."
  [[ -z "$pending_state" || "$pending_state" == "pending" ]] || \
    die "Pending release is not a legacy pending record."
  [[ -z "$current_state" ]] || \
    die "Current release is already structured; do not use the legacy reconciliation flow."
  [[ "$current_tag" == "$pending_previous_tag" && "$current_commit" == "$pending_previous_commit" ]] || \
    die "Current release does not match the interrupted release's recorded predecessor."

  # A blank legacy backup field never implies an empty environment. Require a
  # fresh, validated paired backup before the active marker can be archived.
  uploads_volume_exists || die "Legacy reconciliation refuses a missing uploads volume."
  postgres_volume_exists || die "Legacy reconciliation refuses a missing PostgreSQL volume."
  require_legacy_compose_volume "$postgres_volume_name" postgres_data
  require_legacy_runtime_quiescent
  db_container="$(legacy_db_container)"
  require_legacy_container_volume_mount db "$postgres_volume_name" /var/lib/postgresql/data
  require_legacy_container_volume_mount api "$uploads_volume_name" /var/lib/resume-v3/uploads
  require_legacy_container_volume_mount worker "$uploads_volume_name" /var/lib/resume-v3/uploads
  require_legacy_uploads_volume_provenance
  migrate_state="$(legacy_migrate_state)"
  backup_id="$(create_legacy_reconciliation_backup "$db_container" "$history_dir" \
    "$pending_tag" "$pending_commit" "$current_tag" "$current_commit")"
  require_safe_backup_id "$backup_id"
  # Re-check after the snapshot so a concurrent manual service start cannot
  # make us archive an active pending record based on a writable snapshot.
  require_legacy_runtime_quiescent

  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  archive="$history_dir/releases/interrupted-$timestamp-$pending_tag.env"
  temporary_archive="$history_dir/releases/.interrupted-$timestamp-$pending_tag.partial"
  [[ ! -e "$archive" && ! -e "$temporary_archive" ]] || \
    die "Legacy reconciliation archive path already exists; retry."
  umask 077
  {
    cat "$pending_record"
    cat <<EOF
state=interrupted
interruption_reason=legacy_pending_reconciled_after_verified_paired_backup
reconciliation_backup_id=$backup_id
observed_current_tag=$current_tag
observed_current_commit=$current_commit
observed_migrate_state=$migrate_state
reconciled_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  } > "$temporary_archive"
  mv "$temporary_archive" "$archive"
  rm -f "$pending_record" || die "Unable to clear archived legacy pending release metadata."
  printf 'Legacy pending release archived: %s (paired backup: %s)\n' "$archive" "$backup_id"
}

reconcile_legacy_pending() {
  local environment_dir="$1" history_dir="$2"
  shift 2
  with_release_lock "$history_dir" reconcile_legacy_pending_unlocked \
    "$environment_dir" "$history_dir" "$@"
}

case "${1:-}" in
  release)
    shift
    release "$@"
    ;;
  restore)
    shift
    restore "$@"
    ;;
  reconcile-legacy-pending)
    shift
    reconcile_legacy_pending "$@"
    ;;
  *)
    echo "Usage: $0 {release|restore|reconcile-legacy-pending} <release arguments>" >&2
    exit 2
    ;;
esac
