"""Run Docker-backed release regressions with only synthetic, temporary data.

Examples:

    python scripts/run_release_regression.py --documents
    python scripts/run_release_regression.py --postgres
    python scripts/run_release_regression.py --all

The runner deliberately creates a dedicated Docker network, two throwaway
PostgreSQL containers, and short-lived application containers.  It does not
use compose, production environment files, existing Docker volumes, ports, or
host data directories.
"""
from __future__ import annotations

import argparse
import hashlib
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_RUNNER = REPOSITORY_ROOT / "scripts" / "runtime_release_regression.py"
REGRESSION_LABEL = "com.greatsell.release-regression"
REGRESSION_RUN_LABEL = "com.greatsell.release-regression.run"


class RegressionFailure(RuntimeError):
    """A command failed without exposing an ephemeral database credential."""


@dataclass(frozen=True)
class RuntimeResources:
    run_label: str
    network: str
    source_postgres: str
    restored_postgres: str
    seed_container: str
    source_uploads_volume: str
    restored_uploads_volume: str


def _redact(value: str, *, secrets_to_redact: tuple[str, ...]) -> str:
    redacted = value
    for secret in secrets_to_redact:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _run(
    arguments: list[str],
    *,
    label: str,
    secrets_to_redact: tuple[str, ...] = (),
    input_bytes: bytes | None = None,
    capture: bool = True,
) -> str:
    completed = subprocess.run(
        arguments,
        cwd=REPOSITORY_ROOT,
        input=input_bytes,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if completed.returncode != 0:
        details = ""
        if capture:
            output = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
            if output:
                details = ": " + _redact(output[-500:], secrets_to_redact=secrets_to_redact)
        raise RegressionFailure(f"{label}_failed_exit_{completed.returncode}{details}")
    return (completed.stdout or b"").decode("utf-8", errors="replace").strip()


def _docker(
    *arguments: str,
    label: str,
    secrets_to_redact: tuple[str, ...] = (),
    input_bytes: bytes | None = None,
    capture: bool = True,
) -> str:
    return _run(
        ["docker", *arguments],
        label=label,
        secrets_to_redact=secrets_to_redact,
        input_bytes=input_bytes,
        capture=capture,
    )


def _assert_docker_available() -> None:
    _docker("version", "--format", "{{.Server.Version}}", label="docker_unavailable")


def _resource_label_arguments(run_label: str) -> tuple[str, ...]:
    """Tag every harness-owned Docker resource for safe post-crash cleanup."""

    return (
        "--label",
        f"{REGRESSION_LABEL}=true",
        "--label",
        f"{REGRESSION_RUN_LABEL}={run_label}",
    )


def _build_image(tag: str, *, run_label: str) -> None:
    _docker(
        "build",
        *_resource_label_arguments(run_label),
        "--build-arg",
        "DEBIAN_MIRROR=deb.debian.org",
        "--build-arg",
        "PIP_INDEX_URL=https://pypi.org/simple",
        "--tag",
        tag,
        ".",
        label="application_image_build",
        capture=False,
    )


def _runtime_mount() -> str:
    # --mount accepts Windows drive paths without the colon ambiguity of -v.
    return f"type=bind,src={RUNTIME_RUNNER.parent},dst=/release-regression-scripts,readonly"


def _volume_mount(*, volume_name: str, destination: str, readonly: bool = False) -> str:
    value = f"type=volume,src={volume_name},dst={destination}"
    return f"{value},readonly" if readonly else value


def _run_document_regression(image: str, prefix: str) -> None:
    _docker(
        "run",
        "--rm",
        "--name",
        f"{prefix}-documents",
        *_resource_label_arguments(prefix),
        # Document extraction does not need a database or external network.
        # Make the no-server boundary an enforced Docker property instead of
        # relying only on the harness code never opening a socket.
        "--network",
        "none",
        "--mount",
        _runtime_mount(),
        image,
        "python",
        "/release-regression-scripts/runtime_release_regression.py",
        "documents",
        label="container_document_regression",
        capture=False,
    )


def _wait_for_postgres(container_name: str, *, database_password: str) -> None:
    """Require two successful SQL probes before using a new database.

    ``pg_isready`` alone can briefly report ready while an init/restart edge is
    still closing the server. A real query twice in succession prevents the
    backup/restore exercise from accepting that transient state as healthy.
    """

    deadline = time.monotonic() + 60
    consecutive_successes = 0
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "psql",
                "-U",
                "regression",
                "-d",
                "regression",
                "-Atqc",
                "SELECT 1",
            ],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == b"1":
            consecutive_successes += 1
            if consecutive_successes >= 2:
                return
        else:
            consecutive_successes = 0
        time.sleep(1)
    raise RegressionFailure("temporary_postgres_not_ready")


def _start_postgres(
    *,
    container_name: str,
    network: str,
    database_password: str,
    run_label: str,
) -> None:
    _docker(
        "run",
        "--detach",
        "--name",
        container_name,
        "--network",
        network,
        "--network-alias",
        container_name,
        *_resource_label_arguments(run_label),
        "--env",
        "POSTGRES_DB=regression",
        "--env",
        "POSTGRES_USER=regression",
        "--env",
        f"POSTGRES_PASSWORD={database_password}",
        "postgres:16-alpine",
        label="temporary_postgres_start",
        secrets_to_redact=(database_password,),
    )
    _wait_for_postgres(container_name, database_password=database_password)


def _database_url(hostname: str, password: str) -> str:
    # token_urlsafe uses URL-safe characters, so the generated value can be
    # passed as a PostgreSQL URL component without printing or persisting it.
    return f"postgresql+psycopg://regression:{password}@{hostname}:5432/regression"


def _run_seed(
    *,
    image: str,
    resources: RuntimeResources,
    database_url: str,
    password: str,
) -> None:
    _docker(
        "run",
        "--name",
        resources.seed_container,
        "--network",
        resources.network,
        *_resource_label_arguments(resources.run_label),
        "--mount",
        _runtime_mount(),
        "--mount",
        _volume_mount(
            volume_name=resources.source_uploads_volume,
            destination="/tmp/release-regression/uploads",
        ),
        "--env",
        f"RESUME_V3_DATABASE_URL={database_url}",
        "--env",
        "RELEASE_REGRESSION_UPLOADS_DIR=/tmp/release-regression/uploads",
        image,
        "python",
        "/release-regression-scripts/runtime_release_regression.py",
        "database-seed",
        label="temporary_database_seed",
        secrets_to_redact=(password, database_url),
        capture=False,
    )


def _dump_database(
    *,
    postgres_container: str,
    destination: Path,
    password: str,
) -> None:
    with destination.open("wb") as output:
        completed = subprocess.run(
            [
                "docker",
                "exec",
                postgres_container,
                "pg_dump",
                "-U",
                "regression",
                "-d",
                "regression",
                "-Fc",
            ],
            cwd=REPOSITORY_ROOT,
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
    if completed.returncode != 0:
        details = _redact(
            completed.stderr.decode("utf-8", errors="replace")[-500:],
            secrets_to_redact=(password,),
        )
        raise RegressionFailure(f"temporary_database_dump_failed_exit_{completed.returncode}: {details}")
    if destination.stat().st_size == 0:
        raise RegressionFailure("temporary_database_dump_empty")


def _restore_database(
    *,
    postgres_container: str,
    source: Path,
    password: str,
) -> None:
    command = [
        "docker",
        "exec",
        "--interactive",
        postgres_container,
        "pg_restore",
        "-U",
        "regression",
        "-d",
        "regression",
        "--clean",
        "--if-exists",
        "--no-owner",
    ]
    transient_errors = ("database system is shutting down", "database system is starting up")
    last_details = ""
    for attempt in range(3):
        with source.open("rb") as backup:
            completed = subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                stdin=backup,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
        if completed.returncode == 0:
            return
        last_details = _redact(
            completed.stderr.decode("utf-8", errors="replace")[-500:],
            secrets_to_redact=(password,),
        )
        if attempt < 2 and any(error in last_details.lower() for error in transient_errors):
            _wait_for_postgres(postgres_container, database_password=password)
            continue
        break
    raise RegressionFailure(
        f"temporary_database_restore_failed_exit_{completed.returncode}: {last_details}"
    )


def _create_volume(*, volume_name: str, run_label: str) -> None:
    _docker(
        "volume",
        "create",
        *_resource_label_arguments(run_label),
        volume_name,
        label="temporary_upload_volume_create",
    )
    # Docker creates an empty named volume as root. The production image runs
    # as appuser (UID/GID 10001), so initialize the harness volume explicitly
    # instead of letting a root-owned mount hide a permission regression.
    _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "0",
        *_resource_label_arguments(run_label),
        "--mount",
        _volume_mount(volume_name=volume_name, destination="/uploads"),
        "postgres:16-alpine",
        "sh",
        "-ceu",
        "mkdir -p /uploads && chown -R 10001:10001 /uploads",
        label="temporary_upload_volume_initialize",
        capture=False,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_upload_archive(archive_path: Path) -> None:
    if not archive_path.is_file() or archive_path.stat().st_size == 0:
        raise RegressionFailure("temporary_upload_backup_empty")
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise RegressionFailure("temporary_upload_backup_empty")
        for member in members:
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RegressionFailure("temporary_upload_backup_path_unsafe")


def _archive_uploads_volume(
    *,
    volume_name: str,
    archive_path: Path,
    run_label: str,
) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    _docker(
        "run",
        "--rm",
        "--network",
        "none",
        *_resource_label_arguments(run_label),
        "--mount",
        _volume_mount(volume_name=volume_name, destination="/source", readonly=True),
        "--mount",
        f"type=bind,src={archive_path.parent},dst=/backup",
        "postgres:16-alpine",
        "sh",
        "-ceu",
        "tar -C /source -czf /backup/uploads.tar.gz .",
        label="temporary_upload_volume_archive",
        capture=False,
    )
    _validate_upload_archive(archive_path)


def _restore_uploads_volume(
    *,
    archive_path: Path,
    volume_name: str,
    run_label: str,
) -> None:
    _validate_upload_archive(archive_path)
    _docker(
        "run",
        "--rm",
        "--network",
        "none",
        *_resource_label_arguments(run_label),
        "--mount",
        _volume_mount(volume_name=volume_name, destination="/target"),
        "--mount",
        f"type=bind,src={archive_path.parent},dst=/backup,readonly",
        "postgres:16-alpine",
        "sh",
        "-ceu",
        "tar -xzf /backup/uploads.tar.gz -C /target",
        label="temporary_upload_volume_restore",
        capture=False,
    )


def _create_release_backup_bundle(
    *,
    postgres_container: str,
    uploads_volume: str,
    bundle_root: Path,
    password: str,
    run_label: str,
) -> Path:
    """Write a publish-after-verify paired backup matching production layout."""

    backup_id = f"synthetic-{run_label}"
    staging = bundle_root / f".{backup_id}.partial"
    completed = bundle_root / backup_id
    staging.mkdir(parents=True)
    database_backup = staging / "database.dump"
    uploads_backup = staging / "uploads.tar.gz"
    try:
        _dump_database(
            postgres_container=postgres_container,
            destination=database_backup,
            password=password,
        )
        _archive_uploads_volume(
            volume_name=uploads_volume,
            archive_path=uploads_backup,
            run_label=run_label,
        )
        checksums = {
            "database.dump": _sha256(database_backup),
            "uploads.tar.gz": _sha256(uploads_backup),
        }
        (staging / "checksums.sha256").write_text(
            "".join(f"{digest}  {name}\n" for name, digest in checksums.items()),
            encoding="utf-8",
        )
        (staging / "manifest.env").write_text(
            "\n".join(
                (
                    "format_version=1",
                    "state=complete",
                    f"backup_id={backup_id}",
                    "database_file=database.dump",
                    "uploads_file=uploads.tar.gz",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        _validate_release_backup_bundle(completed=staging, expected_backup_id=backup_id)
        staging.replace(completed)
    except Exception:
        # The temporary directory itself is harness-owned. Never let a partial
        # bundle look like a completed backup in the recovery exercise.
        if staging.exists():
            for child in staging.iterdir():
                child.unlink()
            staging.rmdir()
        raise
    return completed


def _validate_release_backup_bundle(*, completed: Path, expected_backup_id: str) -> None:
    manifest = completed / "manifest.env"
    checksums_path = completed / "checksums.sha256"
    if not manifest.is_file() or not checksums_path.is_file():
        raise RegressionFailure("temporary_release_backup_manifest_missing")
    values = dict(
        line.split("=", maxsplit=1)
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    if (
        values.get("format_version") != "1"
        or values.get("state") != "complete"
        or values.get("backup_id") != expected_backup_id
        or values.get("database_file") != "database.dump"
        or values.get("uploads_file") != "uploads.tar.gz"
    ):
        raise RegressionFailure("temporary_release_backup_manifest_invalid")
    expected_files = {"database.dump", "uploads.tar.gz"}
    checksum_rows = [line.split(maxsplit=1) for line in checksums_path.read_text(encoding="utf-8").splitlines()]
    if len(checksum_rows) != 2 or {row[1].strip() for row in checksum_rows if len(row) == 2} != expected_files:
        raise RegressionFailure("temporary_release_backup_checksums_invalid")
    for row in checksum_rows:
        if len(row) != 2 or _sha256(completed / row[1].strip()) != row[0]:
            raise RegressionFailure("temporary_release_backup_checksum_mismatch")
    _validate_upload_archive(completed / "uploads.tar.gz")


def _run_restore_verify(
    *,
    image: str,
    resources: RuntimeResources,
    database_url: str,
    password: str,
    restored_uploads_volume: str,
) -> None:
    _docker(
        "run",
        "--rm",
        "--name",
        f"{resources.seed_container}-verify",
        "--network",
        resources.network,
        *_resource_label_arguments(resources.run_label),
        "--mount",
        _runtime_mount(),
        "--mount",
        _volume_mount(
            volume_name=restored_uploads_volume,
            destination="/release-regression/uploads",
            readonly=True,
        ),
        "--env",
        f"RESUME_V3_DATABASE_URL={database_url}",
        "--env",
        "RELEASE_REGRESSION_UPLOADS_DIR=/release-regression/uploads",
        image,
        "python",
        "/release-regression-scripts/runtime_release_regression.py",
        "database-verify",
        label="temporary_database_restore_verify",
        secrets_to_redact=(password, database_url),
        capture=False,
    )


def _remove_if_present(*arguments: str) -> None:
    subprocess.run(
        ["docker", *arguments],
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _run_postgres_recovery(image: str, prefix: str) -> None:
    """Migrate, back up, restore and reclaim one expired queue lease."""

    password = secrets.token_urlsafe(24)
    resources = RuntimeResources(
        run_label=prefix,
        network=f"{prefix}-network",
        source_postgres=f"{prefix}-postgres-source",
        restored_postgres=f"{prefix}-postgres-restored",
        seed_container=f"{prefix}-seed",
        source_uploads_volume=f"{prefix}-uploads-source",
        restored_uploads_volume=f"{prefix}-uploads-restored",
    )
    _docker(
        "network",
        "create",
        "--internal",
        *_resource_label_arguments(resources.run_label),
        resources.network,
        label="temporary_network_create",
    )
    try:
        _start_postgres(
            container_name=resources.source_postgres,
            network=resources.network,
            database_password=password,
            run_label=resources.run_label,
        )
        _create_volume(
            volume_name=resources.source_uploads_volume,
            run_label=resources.run_label,
        )
        source_url = _database_url(resources.source_postgres, password)
        _run_seed(
            image=image,
            resources=resources,
            database_url=source_url,
            password=password,
        )

        with tempfile.TemporaryDirectory(prefix="greatsell-postgres-recovery-") as temporary:
            root = Path(temporary)
            backup_dir = _create_release_backup_bundle(
                postgres_container=resources.source_postgres,
                uploads_volume=resources.source_uploads_volume,
                bundle_root=root,
                password=password,
                run_label=resources.run_label,
            )
            # Prove the restore does not accidentally reuse the original
            # database, worker container, or uploads volume after backup.
            _remove_if_present("rm", "--force", resources.seed_container)
            _remove_if_present("rm", "--force", resources.source_postgres)
            _remove_if_present("volume", "rm", resources.source_uploads_volume)
            _start_postgres(
                container_name=resources.restored_postgres,
                network=resources.network,
                database_password=password,
                run_label=resources.run_label,
            )
            _create_volume(
                volume_name=resources.restored_uploads_volume,
                run_label=resources.run_label,
            )
            _restore_uploads_volume(
                archive_path=backup_dir / "uploads.tar.gz",
                volume_name=resources.restored_uploads_volume,
                run_label=resources.run_label,
            )
            _restore_database(
                postgres_container=resources.restored_postgres,
                source=backup_dir / "database.dump",
                password=password,
            )
            restored_url = _database_url(resources.restored_postgres, password)
            _run_restore_verify(
                image=image,
                resources=resources,
                database_url=restored_url,
                password=password,
                restored_uploads_volume=resources.restored_uploads_volume,
            )
    finally:
        _remove_if_present("rm", "--force", resources.seed_container)
        _remove_if_present("rm", "--force", resources.source_postgres)
        _remove_if_present("rm", "--force", resources.restored_postgres)
        _remove_if_present("volume", "rm", resources.source_uploads_volume)
        _remove_if_present("volume", "rm", resources.restored_uploads_volume)
        _remove_if_present("network", "rm", resources.network)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolated Docker release regressions.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--documents", action="store_true", help="Run image-internal document extraction checks.")
    selection.add_argument("--postgres", action="store_true", help="Run migration, backup, restore and lease recovery checks.")
    selection.add_argument("--all", action="store_true", help="Run both release runtime checks (the default).")
    parser.add_argument(
        "--image",
        help="Use an already-built application image instead of building a temporary one.",
    )
    arguments = parser.parse_args()

    _assert_docker_available()
    prefix = f"greatsell-release-regression-{uuid4().hex[:12]}"
    image = arguments.image or f"{prefix}:latest"
    built_image = arguments.image is None
    try:
        if built_image:
            _build_image(image, run_label=prefix)
        if arguments.documents or arguments.all or not arguments.postgres:
            _run_document_regression(image, prefix)
        if arguments.postgres or arguments.all or not arguments.documents:
            _run_postgres_recovery(image, prefix)
    finally:
        if built_image:
            _remove_if_present("image", "rm", image)

    print("release-runtime-regression: passed")


if __name__ == "__main__":
    try:
        main()
    except RegressionFailure as exc:
        print(f"release-runtime-regression: failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
