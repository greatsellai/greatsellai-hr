#!/usr/bin/env python3
"""Validate a portable production data snapshot before it reaches Docker.

The bootstrap importer deliberately receives only a narrow, data-only bundle:
one PostgreSQL custom-format dump, one uploads archive, a checksum file, and a
non-secret manifest.  Keeping this validation outside the production shell
helper makes the bundle contract directly unit-testable while still allowing
the helper to revalidate it immediately before import and again before the
first production promotion.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import tarfile
from pathlib import Path, PurePosixPath


class BootstrapBundleValidationError(ValueError):
    """Raised when a portable bootstrap snapshot violates its fixed contract."""


_IMPORT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}\Z")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_EXPECTED_FILES = (
    "checksums.sha256",
    "database.dump",
    "manifest.env",
    "uploads.tar.gz",
)
_REQUIRED_MANIFEST = {
    "format_version": "1",
    "state": "complete",
    "snapshot_kind": "production_bootstrap",
    "source_environment": "production",
    "source_compose_project": "resume-screening-v3",
    "database_file": "database.dump",
    "uploads_file": "uploads.tar.gz",
}
_OPTIONAL_MANIFEST_KEYS = {
    "created_at",
    "source_release_commit",
    "source_release_tag",
}
_RELEASE_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_RELEASE_TAG_RE = re.compile(r"prod-[0-9]{8}-[0-9a-f]{7,40}\Z")


def _fail(code: str) -> BootstrapBundleValidationError:
    # Keep failure text intentionally generic: bundle contents can include
    # candidate originals, so callers should not echo arbitrary archive paths
    # or manifest values into CI logs.
    return BootstrapBundleValidationError(f"production_bootstrap_bundle_{code}")


def _parse_manifest(path: Path) -> dict[str, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _fail("manifest_unreadable") from exc
    except UnicodeDecodeError as exc:
        raise _fail("manifest_not_utf8") from exc

    values: dict[str, str] = {}
    for line in raw.splitlines():
        if not line or "=" not in line:
            raise _fail("manifest_invalid")
        key, value = line.split("=", maxsplit=1)
        if not key or key in values or "\r" in value or "\n" in value:
            raise _fail("manifest_invalid")
        values[key] = value

    allowed = set(_REQUIRED_MANIFEST) | _OPTIONAL_MANIFEST_KEYS | {"import_id"}
    if set(values) - allowed:
        raise _fail("manifest_unexpected_key")
    if any(values.get(key) != value for key, value in _REQUIRED_MANIFEST.items()):
        raise _fail("manifest_invalid")
    if not _IMPORT_ID_RE.fullmatch(values.get("import_id", "")):
        raise _fail("manifest_import_id_invalid")
    if "created_at" not in values or not _TIMESTAMP_RE.fullmatch(values["created_at"]):
        raise _fail("manifest_timestamp_invalid")
    if "source_release_commit" in values and not _RELEASE_COMMIT_RE.fullmatch(
        values["source_release_commit"]
    ):
        raise _fail("manifest_release_commit_invalid")
    if "source_release_tag" in values and not _RELEASE_TAG_RE.fullmatch(
        values["source_release_tag"]
    ):
        raise _fail("manifest_release_tag_invalid")
    if (
        "source_release_tag" in values
        and "source_release_commit" in values
        and not values["source_release_commit"].startswith(
            values["source_release_tag"].rsplit("-", maxsplit=1)[1]
        )
    ):
        raise _fail("manifest_release_identity_inconsistent")
    return values


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _fail("artifact_unreadable") from exc
    return digest.hexdigest()


def _validate_checksums(bundle_dir: Path) -> None:
    checksums = bundle_dir / "checksums.sha256"
    try:
        actual = checksums.read_text(encoding="ascii")
    except OSError as exc:
        raise _fail("checksums_unreadable") from exc
    except UnicodeDecodeError as exc:
        raise _fail("checksums_not_ascii") from exc

    expected = "".join(
        f"{_sha256(bundle_dir / filename)}  {filename}\n"
        for filename in ("database.dump", "uploads.tar.gz")
    )
    if actual != expected:
        raise _fail("checksum_mismatch")


def _validate_upload_archive(path: Path) -> None:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = archive.getmembers()
    except (OSError, tarfile.TarError) as exc:
        raise _fail("uploads_archive_invalid") from exc

    # A production database can legitimately have no retained originals yet.
    # An empty archive is therefore valid; each member that is present still
    # has to be a regular file or directory under a safe relative path.
    for member in members:
        member_path = PurePosixPath(member.name)
        if (
            member_path.is_absolute()
            or ".." in member_path.parts
            or not (member.isfile() or member.isdir())
        ):
            raise _fail("uploads_archive_unsafe")


def validate_bundle(*, bundle_dir: Path, import_id: str) -> None:
    """Validate the fixed, non-secret production bootstrap bundle contract."""

    if not _IMPORT_ID_RE.fullmatch(import_id):
        raise _fail("import_id_invalid")
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        raise _fail("directory_invalid")

    try:
        entries = tuple(sorted(entry.name for entry in bundle_dir.iterdir()))
    except OSError as exc:
        raise _fail("directory_unreadable") from exc
    if entries != _EXPECTED_FILES:
        raise _fail("files_invalid")

    for filename in _EXPECTED_FILES:
        candidate = bundle_dir / filename
        if not candidate.is_file() or candidate.is_symlink() or candidate.stat().st_size <= 0:
            raise _fail("artifact_invalid")

    manifest = _parse_manifest(bundle_dir / "manifest.env")
    if manifest["import_id"] != import_id:
        raise _fail("import_id_mismatch")
    _validate_checksums(bundle_dir)
    _validate_upload_archive(bundle_dir / "uploads.tar.gz")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a fixed-contract production bootstrap data bundle."
    )
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--import-id", required=True)
    arguments = parser.parse_args()
    try:
        validate_bundle(bundle_dir=arguments.bundle_dir, import_id=arguments.import_id)
    except BootstrapBundleValidationError as exc:
        print(str(exc))
        return 1
    print("production_bootstrap_bundle_valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
