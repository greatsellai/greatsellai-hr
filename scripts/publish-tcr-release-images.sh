#!/usr/bin/env bash
# Publish the two CI-built release images to TCR and record the immutable
# registry manifest references. Deployment environments consume only the
# resulting repo@sha256 references, never a mutable registry tag.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/publish-tcr-release-images.sh <commit-sha> [options]

Options:
  --registry <host>                Required TCR registry hostname
  --namespace <name>               Required TCR namespace
  --repository <owner/repository>  Required GitHub repository identity
  --ci-run-id <id>                 Required CI workflow run ID
  --ci-run-attempt <number>        Required CI workflow run attempt
  --artifact-dir <path>            Required destination for small signed-by-SHA metadata

The caller must already have authenticated Docker to the supplied registry.
The command verifies the locally built OCI labels, pushes unique CI tags, then
records each exact registry manifest digest and image config identity. It never
reads a registry password or writes a deployment environment file.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

require_registry() {
  local registry="$1"
  [[ "$registry" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || \
    die "Invalid TCR registry hostname."
}

require_namespace() {
  local namespace="$1"
  [[ "$namespace" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] || die "Invalid TCR namespace."
}

image_label() {
  local image="$1" label="$2"
  docker image inspect --format "{{ index .Config.Labels \"$label\" }}" "$image"
}

image_config_digest() {
  docker image inspect --format '{{.Id}}' "$1"
}

verify_local_image() {
  local image="$1" release_commit="$2" ci_run_id="$3" ci_run_attempt="$4"
  local config_digest revision image_run_id image_run_attempt
  config_digest="$(image_config_digest "$image")" || die "Required CI image is unavailable: $image"
  [[ "$config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "CI image config identity is malformed: $image"
  revision="$(image_label "$image" org.opencontainers.image.revision)"
  image_run_id="$(image_label "$image" org.opencontainers.image.workflow_run_id)"
  image_run_attempt="$(image_label "$image" org.opencontainers.image.workflow_run_attempt)"
  [[ "$revision" == "$release_commit" ]] || die "CI image revision does not match the release commit: $image"
  [[ "$image_run_id" == "$ci_run_id" ]] || die "CI image run ID does not match: $image"
  [[ "$image_run_attempt" == "$ci_run_attempt" ]] || die "CI image run attempt does not match: $image"
  printf '%s' "$config_digest"
}

resolve_registry_image() {
  local remote_tag="$1" repository="$2" candidate digest
  local -a matches=()

  # Refresh the local digest mapping from the registry after push. This also
  # fails closed if a registry cannot serve the just-pushed unique CI tag.
  docker pull "$remote_tag" >/dev/null
  while IFS= read -r candidate; do
    [[ "$candidate" == "$repository@sha256:"* ]] || continue
    digest="${candidate#"$repository"@}"
    [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Registry returned a malformed image digest."
    matches+=("$candidate")
  done < <(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$remote_tag")

  mapfile -t matches < <(printf '%s\n' "${matches[@]:-}" | sed '/^$/d' | sort -u)
  [[ "${#matches[@]}" == "1" ]] || die "Unable to resolve exactly one immutable registry digest for $repository."
  printf '%s' "${matches[0]}"
}

release_commit="${1:-}"
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || { usage >&2; exit 1; }
shift || true

registry=""
namespace=""
repository=""
ci_run_id=""
ci_run_attempt=""
artifact_dir=""
while (($#)); do
  case "$1" in
    --registry) registry="${2:?--registry requires a value}"; shift 2 ;;
    --namespace) namespace="${2:?--namespace requires a value}"; shift 2 ;;
    --repository) repository="${2:?--repository requires a value}"; shift 2 ;;
    --ci-run-id) ci_run_id="${2:?--ci-run-id requires a value}"; shift 2 ;;
    --ci-run-attempt) ci_run_attempt="${2:?--ci-run-attempt requires a value}"; shift 2 ;;
    --artifact-dir) artifact_dir="${2:?--artifact-dir requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

require_registry "$registry"
require_namespace "$namespace"
[[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "Invalid GitHub repository identity."
[[ "$ci_run_id" =~ ^[0-9]+$ ]] || die "Missing or invalid CI workflow run ID."
[[ "$ci_run_attempt" =~ ^[1-9][0-9]*$ ]] || die "Missing or invalid CI workflow run attempt."
[[ -n "$artifact_dir" ]] || die "Missing metadata artifact directory."

api_image="greatsellai-hr-api:$release_commit"
caddy_image="greatsellai-hr-caddy:$release_commit"
api_config_digest="$(verify_local_image "$api_image" "$release_commit" "$ci_run_id" "$ci_run_attempt")"
caddy_config_digest="$(verify_local_image "$caddy_image" "$release_commit" "$ci_run_id" "$ci_run_attempt")"

publish_tag="ci-${release_commit}-${ci_run_id}-${ci_run_attempt}"
api_repository="$registry/$namespace/hr-api"
caddy_repository="$registry/$namespace/hr-caddy"
api_remote_tag="$api_repository:$publish_tag"
caddy_remote_tag="$caddy_repository:$publish_tag"

docker tag "$api_image" "$api_remote_tag"
docker tag "$caddy_image" "$caddy_remote_tag"
docker push "$api_remote_tag"
docker push "$caddy_remote_tag"

api_registry_image="$(resolve_registry_image "$api_remote_tag" "$api_repository")"
caddy_registry_image="$(resolve_registry_image "$caddy_remote_tag" "$caddy_repository")"

# Recheck the locally pulled tags after resolving registry digests. Image labels
# and config identity bind the manifest reference to this exact proven CI build.
[[ "$(verify_local_image "$api_remote_tag" "$release_commit" "$ci_run_id" "$ci_run_attempt")" == "$api_config_digest" ]] || \
  die "TCR API image config differs from the CI-built image."
[[ "$(verify_local_image "$caddy_remote_tag" "$release_commit" "$ci_run_id" "$ci_run_attempt")" == "$caddy_config_digest" ]] || \
  die "TCR Caddy image config differs from the CI-built image."

metadata_name="release-image-metadata-${release_commit}-${ci_run_id}-${ci_run_attempt}.env"
checksum_name="${metadata_name}.sha256"
install -d "$artifact_dir"
umask 077
cat > "$artifact_dir/$metadata_name" <<EOF_METADATA
format_version=1
repository=$repository
release_sha=$release_commit
ci_run_id=$ci_run_id
ci_run_attempt=$ci_run_attempt
api_registry_image=$api_registry_image
caddy_registry_image=$caddy_registry_image
api_image_config_digest=$api_config_digest
caddy_image_config_digest=$caddy_config_digest
EOF_METADATA
(
  cd "$artifact_dir"
  sha256sum "$metadata_name" > "$checksum_name"
)

echo "Published immutable TCR release images for $release_commit."
