from __future__ import annotations

import hashlib
import io
import tarfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_validator():
    specification = spec_from_file_location(
        "production_bootstrap_bundle_validator",
        ROOT / "scripts" / "validate_production_bootstrap_bundle.py",
    )
    assert specification is not None and specification.loader is not None
    module = module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_upload_archive(path: Path, *, member_name: str = "originals/source.pdf") -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        directory = tarfile.TarInfo("originals")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        payload = b"synthetic-original"
        member = tarfile.TarInfo(member_name)
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))


def _write_bundle(
    root: Path,
    *,
    import_id: str = "move-20260803",
    source_environment: str = "production",
    source_release_tag: str | None = None,
    source_release_commit: str | None = None,
) -> Path:
    root.mkdir()
    database = root / "database.dump"
    uploads = root / "uploads.tar.gz"
    database.write_bytes(b"synthetic-postgres-custom-format")
    _write_upload_archive(uploads)
    manifest = [
        "format_version=1",
        "state=complete",
        "snapshot_kind=production_bootstrap",
        f"import_id={import_id}",
        f"source_environment={source_environment}",
        "source_compose_project=resume-screening-v3",
        "database_file=database.dump",
        "uploads_file=uploads.tar.gz",
        "created_at=2026-08-03T12:34:56Z",
    ]
    if source_release_tag is not None:
        manifest.append(f"source_release_tag={source_release_tag}")
    if source_release_commit is not None:
        manifest.append(f"source_release_commit={source_release_commit}")
    (root / "manifest.env").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    (root / "checksums.sha256").write_text(
        f"{_sha256(database)}  database.dump\n{_sha256(uploads)}  uploads.tar.gz\n",
        encoding="ascii",
    )
    return root


def test_valid_production_bootstrap_bundle_is_accepted(tmp_path: Path) -> None:
    validator = _load_validator()
    bundle = _write_bundle(
        tmp_path / "bundle",
        source_release_tag="prod-20260803-0123456",
        source_release_commit="0123456789abcdef0123456789abcdef01234567",
    )

    validator.validate_bundle(bundle_dir=bundle, import_id="move-20260803")


def test_bundle_accepts_an_empty_uploads_archive(tmp_path: Path) -> None:
    validator = _load_validator()
    bundle = _write_bundle(tmp_path / "bundle")
    with tarfile.open(bundle / "uploads.tar.gz", mode="w:gz"):
        pass
    (bundle / "checksums.sha256").write_text(
        f"{_sha256(bundle / 'database.dump')}  database.dump\n"
        f"{_sha256(bundle / 'uploads.tar.gz')}  uploads.tar.gz\n",
        encoding="ascii",
    )

    validator.validate_bundle(bundle_dir=bundle, import_id="move-20260803")


def test_bundle_rejects_non_production_source_metadata(tmp_path: Path) -> None:
    validator = _load_validator()
    bundle = _write_bundle(tmp_path / "bundle", source_environment="staging")

    with pytest.raises(validator.BootstrapBundleValidationError, match="manifest_invalid"):
        validator.validate_bundle(bundle_dir=bundle, import_id="move-20260803")


def test_bundle_rejects_tampered_checksum(tmp_path: Path) -> None:
    validator = _load_validator()
    bundle = _write_bundle(tmp_path / "bundle")
    (bundle / "database.dump").write_bytes(b"tampered")

    with pytest.raises(validator.BootstrapBundleValidationError, match="checksum_mismatch"):
        validator.validate_bundle(bundle_dir=bundle, import_id="move-20260803")


def test_bundle_rejects_archive_path_traversal_even_with_matching_checksum(tmp_path: Path) -> None:
    validator = _load_validator()
    bundle = _write_bundle(tmp_path / "bundle")
    _write_upload_archive(bundle / "uploads.tar.gz", member_name="../outside")
    (bundle / "checksums.sha256").write_text(
        f"{_sha256(bundle / 'database.dump')}  database.dump\n"
        f"{_sha256(bundle / 'uploads.tar.gz')}  uploads.tar.gz\n",
        encoding="ascii",
    )

    with pytest.raises(validator.BootstrapBundleValidationError, match="uploads_archive_unsafe"):
        validator.validate_bundle(bundle_dir=bundle, import_id="move-20260803")


def test_bundle_rejects_inconsistent_source_release_identity(tmp_path: Path) -> None:
    validator = _load_validator()
    bundle = _write_bundle(
        tmp_path / "bundle",
        source_release_tag="prod-20260803-0123456",
        source_release_commit="fedcba9876543210fedcba9876543210fedcba98",
    )

    with pytest.raises(
        validator.BootstrapBundleValidationError,
        match="manifest_release_identity_inconsistent",
    ):
        validator.validate_bundle(bundle_dir=bundle, import_id="move-20260803")


def test_bundle_rejects_any_extra_file(tmp_path: Path) -> None:
    validator = _load_validator()
    bundle = _write_bundle(tmp_path / "bundle")
    (bundle / "unexpected.txt").write_text("not allowed", encoding="utf-8")

    with pytest.raises(validator.BootstrapBundleValidationError, match="files_invalid"):
        validator.validate_bundle(bundle_dir=bundle, import_id="move-20260803")
