from __future__ import annotations

import hashlib
import io
import os
import tarfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STAGER_PATH = REPOSITORY_ROOT / "scripts" / "release_source_stage.py"
COMMIT = "a" * 40


def _load_stager():
    specification = spec_from_file_location("release_source_stage", STAGER_PATH)
    assert specification is not None and specification.loader is not None
    module = module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _archive_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, payload in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(payload)
            entry.mode = 0o755 if name.endswith(".sh") else 0o644
            archive.addfile(entry, io.BytesIO(payload))
    return output.getvalue()


def _release_archive(*, obsolete: bool = False) -> bytes:
    files = {
        "compose.yml": b"name: release-test\n",
        "Dockerfile": b"FROM scratch\n",
        "deploy/Caddy.Dockerfile": b"FROM scratch\n",
        "scripts/remote-release-helper.sh": b"#!/usr/bin/env bash\n",
        "app/current.py": b"current\n",
    }
    if obsolete:
        files["app/obsolete.py"] = b"obsolete\n"
    return _archive_bytes(files)


def _stage(stager, archive: bytes, root: Path, commit: str = COMMIT) -> Path:
    return stager.stage_release_archive(
        io.BytesIO(archive),
        release_root=root,
        release_commit=commit,
        expected_sha256=hashlib.sha256(archive).hexdigest(),
    )


def _require_posix_symlink_support() -> None:
    if os.name != "posix":
        pytest.skip("atomic symlink activation is exercised in Linux CI")


def test_stage_publishes_an_immutable_clean_source_tree(tmp_path: Path) -> None:
    _require_posix_symlink_support()
    stager = _load_stager()
    release_root = tmp_path / "release-sources"
    old_commit = "b" * 40
    old_source = _stage(stager, _release_archive(obsolete=True), release_root, old_commit)
    pointer = stager.activate_release_source(release_root=release_root, source_dir=old_source)

    new_source = _stage(stager, _release_archive(), release_root)

    assert (old_source / "app" / "obsolete.py").is_file()
    assert not (new_source / "app" / "obsolete.py").exists()
    assert pointer.resolve() == old_source

    stager.activate_release_source(release_root=release_root, source_dir=new_source)
    assert pointer.resolve() == new_source
    assert not list(release_root.glob(".*.partial-*"))


def test_invalid_archive_never_replaces_current_source_or_leaves_partial_state(tmp_path: Path) -> None:
    _require_posix_symlink_support()
    stager = _load_stager()
    release_root = tmp_path / "release-sources"
    old_source = _stage(stager, _release_archive(obsolete=True), release_root, "b" * 40)
    pointer = stager.activate_release_source(release_root=release_root, source_dir=old_source)
    archive = _archive_bytes(
        {
            "compose.yml": b"name: unsafe\n",
            "../.env.production": b"must-not-extract\n",
        }
    )

    with pytest.raises(stager.ReleaseSourceStageError, match="unsafe_archive_path"):
        _stage(stager, archive, release_root)

    assert pointer.resolve() == old_source
    assert not (release_root / COMMIT).exists()
    assert not list(release_root.glob(".*.partial-*"))


def test_existing_source_is_reusable_only_for_the_same_verified_archive(tmp_path: Path) -> None:
    stager = _load_stager()
    release_root = tmp_path / "release-sources"
    archive = _release_archive()
    source = _stage(stager, archive, release_root)

    assert _stage(stager, archive, release_root) == source
    different_archive = _release_archive(obsolete=True)
    with pytest.raises(stager.ReleaseSourceStageError, match="existing_release_source_mismatch"):
        _stage(stager, different_archive, release_root)
