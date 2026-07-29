#!/usr/bin/env bash
# Keep the exact staging edge route available after a production Caddy
# recreation, including a rollback to a Caddy image created before staging was
# versioned. This touches only the running public Caddy configuration; it does
# not read any environment file, modify application code, or expose a root/
# wildcard domain.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/ensure-staging-gateway.sh --host <ssh-host> [options]

Options:
  --ssh-key <path>  Optional SSH private-key path; never committed

The target must already have a running production Caddy container labelled
com.docker.compose.project=resume-screening-v3. The command no-ops when the
current Caddy image already contains the versioned staging route.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

shell_quote() {
  printf '%q' "$1"
}

remote_host=""
ssh_key=""
while (($#)); do
  case "$1" in
    --host) remote_host="${2:?--host requires a value}"; shift 2 ;;
    --ssh-key) ssh_key="${2:?--ssh-key requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$remote_host" ]] || { usage >&2; die "Missing production Caddy host."; }
[[ -z "$ssh_key" || -r "$ssh_key" ]] || die "SSH key is not readable."

ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=yes)
if [[ -n "$ssh_key" ]]; then
  ssh_options+=(-i "$ssh_key" -o IdentitiesOnly=yes)
fi

remote_script="$(cat <<'EOF'
set -Eeuo pipefail

umask 077
exec 9>/tmp/greatsell-staging-gateway.lock
flock 9

container="$(sudo -n docker ps --filter 'label=com.docker.compose.project=resume-screening-v3' --filter 'label=com.docker.compose.service=caddy' --format '{{.ID}}')"
[[ -n "$container" && "$(printf '%s\n' "$container" | wc -l | tr -d ' ')" == "1" ]] || {
  echo "Expected exactly one running production Caddy container." >&2
  exit 1
}

# New release images carry the route directly in their reviewed Caddyfile.
if sudo -n docker exec "$container" sh -ceu 'grep -Fq "staging.hr.greatsellai.net" /etc/caddy/Caddyfile'; then
  echo "Versioned staging gateway route is already present in the Caddy image."
  exit 0
fi

# Older production tags do not know about the staging origin. Overlay their
# baked configuration after every recreation, instead of changing DNS or
# touching the root marketing site. A failed validation leaves the existing
# Caddy process/configuration untouched.
sudo -n docker exec -i "$container" sh -ceu '
  umask 077
  temporary=/config/Caddyfile.staging-gateway.$$.tmp
  final=/config/Caddyfile.staging-gateway
  cat > "$temporary"
  caddy validate --config "$temporary" --adapter caddyfile >/dev/null
  mv -f "$temporary" "$final"
  caddy reload --config "$final" --adapter caddyfile
' <<'CADDYFILE'
import /etc/caddy/Caddyfile

staging.hr.greatsellai.net {
	encode zstd gzip
	reverse_proxy 172.17.0.1:18080
}
CADDYFILE

echo "Restored exact staging gateway route for a legacy Caddy image."
EOF
)"

ssh "${ssh_options[@]}" "$remote_host" "bash -c $(shell_quote "$remote_script")"
