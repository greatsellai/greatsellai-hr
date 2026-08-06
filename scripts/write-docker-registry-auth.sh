#!/usr/bin/env bash
# Write a short-lived Docker auth config without invoking `docker login`.
# Tencent Cloud personal TCR can issue valid scoped bearer tokens while some
# Docker clients reject its unscoped login probe. Docker itself will use this
# standard Basic credential entry when it requests the scoped push/pull token.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/write-docker-registry-auth.sh [options] --password-stdin

Options:
  --registry <host>        Required Docker registry hostname
  --username <name>        Required Docker registry username
  --docker-config <path>   Required empty, private Docker config directory
  --password-stdin         Read exactly one registry password line from stdin

The command creates <docker-config>/config.json with mode 0600. It neither
logs in nor contacts a registry; callers must securely delete the supplied
temporary directory after their Docker operation completes.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

registry=""
username=""
docker_config=""
password_stdin=0
while (($#)); do
  case "$1" in
    --registry) registry="${2:?--registry requires a value}"; shift 2 ;;
    --username) username="${2:?--username requires a value}"; shift 2 ;;
    --docker-config) docker_config="${2:?--docker-config requires a value}"; shift 2 ;;
    --password-stdin) password_stdin=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ "$registry" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "Invalid registry hostname."
[[ "$username" =~ ^[^[:space:]:]+$ ]] || die "Missing or invalid registry username."
[[ "$password_stdin" == "1" ]] || die "Refusing to read a registry password except from standard input."
[[ -n "$docker_config" && -d "$docker_config" ]] || die "Docker config directory does not exist."
[[ ! -e "$docker_config/config.json" ]] || die "Docker config directory must not already contain config.json."

if ! IFS= read -r registry_password; then
  die "Missing registry password on standard input."
fi
[[ -n "$registry_password" ]] || die "Registry password on standard input is empty."
trap 'unset registry_password auth' EXIT

umask 077
auth="$(printf '%s' "$username:$registry_password" | base64 | tr -d '\n')"
[[ -n "$auth" ]] || die "Unable to encode Docker registry credentials."
printf '{"auths":{"%s":{"auth":"%s"}}}\n' "$registry" "$auth" > "$docker_config/config.json"
chmod 600 "$docker_config/config.json"
unset registry_password
unset auth
