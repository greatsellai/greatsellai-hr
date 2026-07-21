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


def _archive_uploads(source: Path, archive_path: Path) -> None:
    with tarfile.open(archive_path, mode="w:gz") as archive:
        archive.add(source, arcname="uploads")
    if archive_path.stat().st_size == 0:
        raise RegressionFailure("temporary_upload_backup_empty")


def _restore_uploads(archive_path: Path, destination_root: Path) -> Path:
    with tarfile.open(archive_path, mode="r:gz") as archive:
        root = destination_root.resolve()
        for member in archive.getmembers():
            member_target = (destination_root / member.name).resolve()
            if not member_target.is_relative_to(root):
                raise RegressionFailure("temporary_upload_backup_path_unsafe")
        archive.extractall(destination_root, filter="data")
    restored = destination_root / "uploads"
    if not restored.is_dir():
        raise RegressionFailure("temporary_upload_restore_missing")
    return restored


def _copy_seed_uploads(seed_container: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    _docker(
        "cp",
        f"{seed_container}:/tmp/release-regression/uploads/.",
        str(destination),
        label="temporary_upload_copy",
    )
    if not any(destination.rglob("*")):
        raise RegressionFailure("temporary_upload_copy_empty")


def _run_restore_verify(
    *,
    image: str,
    resources: RuntimeResources,
    database_url: str,
    password: str,
    restored_uploads: Path,
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
        f"type=bind,src={restored_uploads},dst=/release-regression/uploads,readonly",
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
        source_url = _database_url(resources.source_postgres, password)
        _run_seed(
            image=image,
            resources=resources,
            database_url=source_url,
            password=password,
        )

        with tempfile.TemporaryDirectory(prefix="greatsell-postgres-recovery-") as temporary:
            root = Path(temporary)
            source_uploads = root / "source-uploads"
            backup_uploads = root / "uploads.tar.gz"
            restored_root = root / "restored"
            database_backup = root / "database.dump"
            _copy_seed_uploads(resources.seed_container, source_uploads)
            _archive_uploads(source_uploads, backup_uploads)
            restored_uploads = _restore_uploads(backup_uploads, restored_root)
            # The read-only appuser in the verification container needs to
            # traverse these synthetic files on Linux CI.  Windows ignores
            # chmod here, which is harmless.
            for path in (restored_root, restored_uploads, *restored_uploads.rglob("*")):
                if path.is_dir():
                    path.chmod(0o755)
                else:
                    path.chmod(0o644)
            _dump_database(
                postgres_container=resources.source_postgres,
                destination=database_backup,
                password=password,
            )
            _start_postgres(
                container_name=resources.restored_postgres,
                network=resources.network,
                database_password=password,
                run_label=resources.run_label,
            )
            _restore_database(
                postgres_container=resources.restored_postgres,
                source=database_backup,
                password=password,
            )
            restored_url = _database_url(resources.restored_postgres, password)
            _run_restore_verify(
                image=image,
                resources=resources,
                database_url=restored_url,
                password=password,
                restored_uploads=restored_uploads,
            )
    finally:
        _remove_if_present("rm", "--force", resources.seed_container)
        _remove_if_present("rm", "--force", resources.source_postgres)
        _remove_if_present("rm", "--force", resources.restored_postgres)
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
