#!/usr/bin/env bash
# Validate and load one CI-produced release-image artifact without rebuilding
# any application layer. Staging and production use the same verifier so the
# immutable artifact's integrity and provenance checks cannot drift.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/load-verified-release-images.sh <commit-sha> [options]

Options:
  --artifact-dir <path>             Required directory downloaded from GitHub Actions
  --repository <owner/repository>   Required repository recorded in artifact metadata
  --ci-run-id <id>                  Required CI workflow run ID
  --ci-run-attempt <number>         Required CI workflow run attempt
  --expected-ci-image-archive-sha256 <sha256>
                                    Optional completed-staging CI archive digest
  --expected-api-image-config-digest <sha256>
                                    Optional completed-staging API image config digest
  --expected-caddy-image-config-digest <sha256>
                                    Optional completed-staging Caddy image config digest
  --github-output <path>            Optional GitHub output file for portable
                                    CI archive and image config identities

The artifact must contain the exact archive, checksum and metadata generated
by Continuous integration. This command verifies all three before loading the
API and Caddy images, then verifies their immutable revision and CI labels.
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

metadata_value() {
  local metadata="$1" key="$2" value count
  count="$(grep -c "^${key}=" "$metadata" || true)"
  [[ "$count" == "1" ]] || die "Release image metadata is missing or repeats '$key'."
  value="$(sed -n "s/^${key}=//p" "$metadata")"
  [[ -n "$value" ]] || die "Release image metadata has an empty '$key'."
  printf '%s' "$value"
}

image_label() {
  local image="$1" label="$2"
  docker image inspect --format "{{ index .Config.Labels \"$label\" }}" "$image"
}

archive_image_config_digests() {
  local archive="$1" api_image="$2" caddy_image="$3"
  python3 -c '
import hashlib
import json
import tarfile
import sys

archive, api_image, caddy_image = sys.argv[1:]
manifest_payloads = []
file_hashes = {}
file_counts = {}
with tarfile.open(archive, "r|*") as tar:
    for item in tar:
        if not item.isfile():
            continue
        stream = tar.extractfile(item)
        if stream is None:
            raise SystemExit(f"Docker image archive member cannot be read: {item.name}")
        if item.name == "manifest.json":
            manifest_payloads.append(stream.read())
            continue
        # A Docker-save config is either the legacy <digest>.json filename or
        # an OCI blob. Hash candidates while the compressed stream is traversed
        # once, so two image identities do not require several full rescans.
        if item.name.endswith(".json") or item.name.startswith("blobs/sha256/"):
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            file_counts[item.name] = file_counts.get(item.name, 0) + 1
            file_hashes[item.name] = digest.hexdigest()
if len(manifest_payloads) != 1:
    raise SystemExit("Docker image archive must contain exactly one manifest.json")
try:
    manifests = json.loads(manifest_payloads[0])
except (TypeError, ValueError) as error:
    raise SystemExit(f"Docker image archive manifest is invalid: {error}")
if not isinstance(manifests, list):
    raise SystemExit("Docker image archive manifest is not a list")

def config_digest_for(image):
    matches = [
        item
        for item in manifests
        if isinstance(item, dict)
        and isinstance(item.get("RepoTags"), list)
        and image in item["RepoTags"]
    ]
    if len(matches) != 1:
        raise SystemExit(f"Docker image archive does not contain exactly one manifest for {image}")
    config = matches[0].get("Config")
    if not isinstance(config, str):
        raise SystemExit(f"Docker image archive config path is invalid for {image}")
    if config.startswith("blobs/sha256/"):
        digest = config.removeprefix("blobs/sha256/")
    elif config.endswith(".json"):
        digest = config.removesuffix(".json")
    else:
        raise SystemExit(f"Docker image archive config path is unsupported for {image}")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise SystemExit(f"Docker image archive config digest is invalid for {image}")
    if file_counts.get(config) != 1:
        raise SystemExit(f"Docker image archive config blob is missing or repeated for {image}")
    if file_hashes.get(config) != digest:
        raise SystemExit(f"Docker image archive config blob digest does not match for {image}")
    return f"sha256:{digest}"

print(config_digest_for(api_image))
print(config_digest_for(caddy_image))
' "$archive" "$api_image" "$caddy_image"
}

release_commit="${1:-}"
[[ "$release_commit" =~ ^[0-9a-f]{40}$ ]] || { usage >&2; exit 1; }
shift || true

artifact_dir=""
repository=""
ci_run_id=""
ci_run_attempt=""
expected_ci_image_archive_sha256=""
expected_api_image_config_digest=""
expected_caddy_image_config_digest=""
github_output=""
while (($#)); do
  case "$1" in
    --artifact-dir) artifact_dir="${2:?--artifact-dir requires a value}"; shift 2 ;;
    --repository) repository="${2:?--repository requires a value}"; shift 2 ;;
    --ci-run-id) ci_run_id="${2:?--ci-run-id requires a value}"; shift 2 ;;
    --ci-run-attempt) ci_run_attempt="${2:?--ci-run-attempt requires a value}"; shift 2 ;;
    --expected-ci-image-archive-sha256) expected_ci_image_archive_sha256="${2:?--expected-ci-image-archive-sha256 requires a value}"; shift 2 ;;
    --expected-api-image-config-digest) expected_api_image_config_digest="${2:?--expected-api-image-config-digest requires a value}"; shift 2 ;;
    --expected-caddy-image-config-digest) expected_caddy_image_config_digest="${2:?--expected-caddy-image-config-digest requires a value}"; shift 2 ;;
    --github-output) github_output="${2:?--github-output requires a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

[[ -n "$artifact_dir" && -d "$artifact_dir" ]] || die "Release image artifact directory is unavailable."
[[ -n "$repository" ]] || die "Missing artifact repository identity."
[[ "$ci_run_id" =~ ^[0-9]+$ ]] || die "Missing or invalid CI workflow run ID."
[[ "$ci_run_attempt" =~ ^[1-9][0-9]*$ ]] || die "Missing or invalid CI workflow run attempt."
[[ -z "$expected_ci_image_archive_sha256" || "$expected_ci_image_archive_sha256" =~ ^[0-9a-f]{64}$ ]] || \
  die "Invalid expected CI image archive checksum."
[[ -z "$expected_api_image_config_digest" || "$expected_api_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || \
  die "Invalid expected API image config digest."
[[ -z "$expected_caddy_image_config_digest" || "$expected_caddy_image_config_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || \
  die "Invalid expected Caddy image config digest."
[[ ( -z "$expected_ci_image_archive_sha256" && -z "$expected_api_image_config_digest" && -z "$expected_caddy_image_config_digest" ) || \
   ( -n "$expected_ci_image_archive_sha256" && -n "$expected_api_image_config_digest" && -n "$expected_caddy_image_config_digest" ) ]] || \
  die "Expected CI archive and image config identities must be provided together."
[[ -z "$github_output" || -e "$github_output" ]] || die "GitHub output file is not available."

archive_name="release-images-${release_commit}-${ci_run_id}-${ci_run_attempt}.tar.gz"
metadata_name="${archive_name%.tar.gz}.metadata"
archive="$artifact_dir/$archive_name"
checksum="$artifact_dir/$archive_name.sha256"
metadata="$artifact_dir/$metadata_name"
[[ -f "$archive" && -f "$checksum" && -f "$metadata" ]] || die "Verified release image artifact is incomplete."
mapfile -t checksum_lines < "$checksum"
[[ "${#checksum_lines[@]}" == "1" ]] || \
  die "Release image artifact checksum does not name the expected archive exactly once."
checksum_hash="${checksum_lines[0]%% *}"
[[ "$checksum_hash" =~ ^[0-9a-f]{64}$ && "${checksum_lines[0]}" == "$checksum_hash  $archive_name" ]] || \
  die "Release image artifact checksum does not name the expected archive exactly once."

(
  cd "$artifact_dir"
  sha256sum --check "$archive_name.sha256"
)
[[ "$(metadata_value "$metadata" repository)" == "$repository" ]] || die "Release image artifact repository does not match."
[[ "$(metadata_value "$metadata" release_sha)" == "$release_commit" ]] || die "Release image artifact release SHA does not match."
[[ "$(metadata_value "$metadata" ci_run_id)" == "$ci_run_id" ]] || die "Release image artifact CI run ID does not match."
[[ "$(metadata_value "$metadata" ci_run_attempt)" == "$ci_run_attempt" ]] || die "Release image artifact CI run attempt does not match."
[[ "$(metadata_value "$metadata" archive)" == "$archive_name" ]] || die "Release image artifact archive name does not match."

api_image="greatsellai-hr-api:$release_commit"
caddy_image="greatsellai-hr-caddy:$release_commit"
image_config_digests="$(archive_image_config_digests "$archive" "$api_image" "$caddy_image")" || \
  die "Unable to read image config identities from the CI archive."
mapfile -t image_config_digest_lines <<< "$image_config_digests"
[[ "${#image_config_digest_lines[@]}" == "2" ]] || \
  die "CI archive did not produce exactly two image config identities."
api_image_config_digest="${image_config_digest_lines[0]}"
caddy_image_config_digest="${image_config_digest_lines[1]}"
[[ -z "$expected_ci_image_archive_sha256" || "$checksum_hash" == "$expected_ci_image_archive_sha256" ]] || \
  die "CI image archive does not match the completed staging attestation."
[[ -z "$expected_api_image_config_digest" || "$api_image_config_digest" == "$expected_api_image_config_digest" ]] || \
  die "API image config does not match the completed staging attestation."
[[ -z "$expected_caddy_image_config_digest" || "$caddy_image_config_digest" == "$expected_caddy_image_config_digest" ]] || \
  die "Caddy image config does not match the completed staging attestation."

docker image load --input "$archive"

for image in "$api_image" "$caddy_image"; do
  [[ "$(image_label "$image" org.opencontainers.image.revision)" == "$release_commit" ]] || \
    die "Loaded image revision does not match the release commit: $image"
  [[ "$(image_label "$image" org.opencontainers.image.workflow_run_id)" == "$ci_run_id" ]] || \
    die "Loaded image CI workflow run ID does not match: $image"
  [[ "$(image_label "$image" org.opencontainers.image.workflow_run_attempt)" == "$ci_run_attempt" ]] || \
    die "Loaded image CI workflow run attempt does not match: $image"
done

api_image_id="$(docker image inspect --format '{{.Id}}' "$api_image")"
caddy_image_id="$(docker image inspect --format '{{.Id}}' "$caddy_image")"
[[ "$api_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Loaded API image ID is malformed."
[[ "$caddy_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "Loaded Caddy image ID is malformed."

# Docker daemon image IDs can refer to different OCI layers after archive
# import, depending on the supported daemon version. The verified archive
# SHA-256 plus each named archive config blob are portable content identities;
# local IDs remain useful only on their own host.

if [[ -n "$github_output" ]]; then
  {
    printf 'ci_image_archive_sha256=%s\n' "$checksum_hash"
    printf 'api_image_config_digest=%s\n' "$api_image_config_digest"
    printf 'caddy_image_config_digest=%s\n' "$caddy_image_config_digest"
  } >> "$github_output"
fi

echo "CI-verified release images loaded for $release_commit."
