#!/usr/bin/env python3
"""Stage a Git archive as an immutable, verified production release source.

This helper deliberately has no Docker, SSH, or environment-file knowledge.
It receives an archive from the already-reviewed release commit, verifies its
digest, expands only regular files/directories into a private staging path, and
publishes the source tree with an atomic rename.  Keeping this small operation
separate makes the critical "do not overwrite the live worktree" property
unit-testable without a production host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO


class ReleaseSourceStageError(RuntimeError):
    """The archive cannot safely become a release source tree."""


_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_FILES = (
    "compose.yml",
    "Dockerfile",
    "deploy/Caddy.Dockerfile",
    "scripts/remote-release-helper.sh",
)
_FORBIDDEN_PATHS = frozenset({".env.production"})
_MANIFEST_NAME = ".greatsell-release-source.json"


def _validate_release_commit(release_commit: str) -> None:
    if not _COMMIT_PATTERN.fullmatch(release_commit):
        raise ReleaseSourceStageError("invalid_release_commit")


def _validate_member_name(name: str) -> Path:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts:
        raise ReleaseSourceStageError("unsafe_archive_path")
    relative = Path(*path.parts)
    if relative.as_posix() in _FORBIDDEN_PATHS:
        raise ReleaseSourceStageError("forbidden_archive_path")
    return relative


def _copy_stream_to_file(source: BinaryIO, destination: Path) -> str:
    digest = hashlib.sha256()
    with destination.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    return digest.hexdigest()


def _extract_archive(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, mode="r:") as archive:
        members = archive.getmembers()
        validated: list[tuple[tarfile.TarInfo, Path]] = []
        for member in members:
            relative = _validate_member_name(member.name)
            if member.isdir() or member.isreg():
                validated.append((member, relative))
                continue
            raise ReleaseSourceStageError("unsupported_archive_member")

        for member, relative in validated:
            output_path = destination / relative
            if member.isdir():
                output_path.mkdir(parents=True, exist_ok=True)
                os.chmod(output_path, member.mode & 0o777)
                continue
            output_path.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseSourceStageError("archive_member_unreadable")
            with source, output_path.open("xb") as output:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(output_path, member.mode & 0o777)


def _validate_release_tree(source_dir: Path) -> None:
    for relative in _REQUIRED_FILES:
        candidate = source_dir / relative
        if not candidate.is_file():
            raise ReleaseSourceStageError(f"release_source_missing_{relative.replace('/', '_')}")
    if (source_dir / ".env.production").exists():
        raise ReleaseSourceStageError("release_source_contains_environment_file")


def _read_manifest(source_dir: Path) -> dict[str, object] | None:
    manifest_path = source_dir / _MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseSourceStageError("release_source_manifest_invalid") from exc
    return payload if isinstance(payload, dict) else None


def stage_release_archive(
    archive_stream: BinaryIO,
    *,
    release_root: Path,
    release_commit: str,
    expected_sha256: str,
) -> Path:
    """Publish a verified archive under ``release_root/<full-commit>``.

    An already-published tree is reusable only when its manifest proves it came
    from the exact same archive.  A failed extraction never changes that tree
    or leaves a discoverable partial directory behind.
    """

    _validate_release_commit(release_commit)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ReleaseSourceStageError("invalid_archive_checksum")

    release_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(release_root, 0o700)
    final_dir = release_root / release_commit
    temporary_root = Path(tempfile.mkdtemp(prefix=f".{release_commit}.partial-", dir=release_root))
    archive_path = temporary_root / "source.tar"
    staging_dir = temporary_root / "source"

    try:
        actual_sha256 = _copy_stream_to_file(archive_stream, archive_path)
        if actual_sha256 != expected_sha256:
            raise ReleaseSourceStageError("archive_checksum_mismatch")

        if final_dir.exists():
            manifest = _read_manifest(final_dir)
            if manifest != {
                "archive_sha256": expected_sha256,
                "release_commit": release_commit,
                "format_version": 1,
            }:
                raise ReleaseSourceStageError("existing_release_source_mismatch")
            _validate_release_tree(final_dir)
            return final_dir

        staging_dir.mkdir(mode=0o700)
        _extract_archive(archive_path, staging_dir)
        _validate_release_tree(staging_dir)
        (staging_dir / _MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "archive_sha256": expected_sha256,
                    "release_commit": release_commit,
                    "format_version": 1,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(staging_dir, final_dir)
        return final_dir
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def activate_release_source(*, release_root: Path, source_dir: Path) -> Path:
    """Atomically update the current-source symlink after a healthy deploy."""

    source_dir = source_dir.resolve(strict=True)
    release_root = release_root.resolve(strict=True)
    try:
        source_dir.relative_to(release_root)
    except ValueError as exc:
        raise ReleaseSourceStageError("source_dir_outside_release_root") from exc
    _validate_release_tree(source_dir)

    pointer = release_root.parent / "current-source"
    temporary_pointer = release_root.parent / f".current-source.{os.getpid()}"
    try:
        temporary_pointer.unlink(missing_ok=True)
        os.symlink(source_dir, temporary_pointer)
        os.replace(temporary_pointer, pointer)
    finally:
        temporary_pointer.unlink(missing_ok=True)
    return pointer


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--release-commit")
    parser.add_argument("--archive-sha256")
    parser.add_argument("--activate-source", type=Path)
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    try:
        if arguments.activate_source is not None:
            if arguments.release_commit or arguments.archive_sha256:
                raise ReleaseSourceStageError("activate_source_arguments_conflict")
            pointer = activate_release_source(
                release_root=arguments.release_root,
                source_dir=arguments.activate_source,
            )
            print(pointer)
            return 0

        if not arguments.release_commit or not arguments.archive_sha256:
            raise ReleaseSourceStageError("stage_arguments_required")
        source_dir = stage_release_archive(
            sys.stdin.buffer,
            release_root=arguments.release_root,
            release_commit=arguments.release_commit,
            expected_sha256=arguments.archive_sha256,
        )
        print(source_dir)
        return 0
    except ReleaseSourceStageError as exc:
        print(f"release_source_stage_error:{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
