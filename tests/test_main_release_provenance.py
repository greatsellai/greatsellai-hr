from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "verify_main_release_provenance.py"
)
SPEC = importlib.util.spec_from_file_location("main_release_provenance", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
PROVENANCE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROVENANCE
SPEC.loader.exec_module(PROVENANCE)


REPOSITORY = "greatsellai/greatsellai-hr"
RELEASE_SHA = "a" * 40
PR_HEAD_SHA = "b" * 40
TREE_SHA = "c" * 40


def _commit(tree_sha: str, *, parent_sha: str = "d" * 40) -> dict[str, object]:
    return {"tree": {"sha": tree_sha}, "parents": [{"sha": parent_sha}]}


def _successful_item(*, name: str, timestamp: str) -> dict[str, object]:
    return {
        "name": name,
        "status": "completed",
        "conclusion": "success",
        "completed_at": timestamp,
    }


def _successful_workflow_run(*, run_id: int, timestamp: str) -> dict[str, object]:
    return {
        "id": run_id,
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "completed_at": timestamp,
    }


def _success_responses() -> dict[str, object]:
    return {
        f"/repos/{REPOSITORY}": {"private": True},
        f"/repos/{REPOSITORY}/commits/{RELEASE_SHA}/pulls?per_page=100": [
            {
                "number": 108,
                "state": "closed",
                "merged_at": "2026-07-27T08:49:00Z",
                "merge_commit_sha": RELEASE_SHA,
                "head": {
                    "sha": PR_HEAD_SHA,
                    "repo": {"full_name": REPOSITORY},
                },
                "base": {
                    "sha": "d" * 40,
                    "ref": "main",
                    "repo": {"full_name": REPOSITORY},
                },
            }
        ],
        f"/repos/{REPOSITORY}/git/commits/{RELEASE_SHA}": _commit(TREE_SHA),
        f"/repos/{REPOSITORY}/git/commits/{PR_HEAD_SHA}": _commit(TREE_SHA),
        (
            f"/repos/{REPOSITORY}/actions/workflows/ci.yml/runs?event=pull_request"
            f"&head_sha={PR_HEAD_SHA}&per_page=100"
        ): {
            "workflow_runs": [
                _successful_workflow_run(run_id=12345, timestamp="2026-07-27T08:48:00Z")
            ]
        },
        f"/repos/{REPOSITORY}/actions/runs/12345/jobs?per_page=100": {
            "jobs": [
                _successful_item(name=name, timestamp="2026-07-27T08:48:00Z")
                for name in sorted(PROVENANCE.REQUIRED_CI_JOB_NAMES)
            ]
        },
        (
            f"/repos/{REPOSITORY}/actions/workflows/{PROVENANCE.TEXT_ENCODING_WORKFLOW_FILE}"
            f"/runs?event=pull_request&head_sha={PR_HEAD_SHA}&per_page=100"
        ): {
            "workflow_runs": [
                _successful_workflow_run(run_id=54321, timestamp="2026-07-27T08:48:30Z")
            ]
        },
        f"/repos/{REPOSITORY}/actions/runs/54321/jobs?per_page=100": {
            "jobs": [
                _successful_item(
                    name=PROVENANCE.REQUIRED_TEXT_CHECK_NAME,
                    timestamp="2026-07-27T08:48:30Z",
                )
            ]
        },
    }


def _fetcher(responses: Mapping[str, object]):
    def fetch_json(path: str) -> object:
        return responses[path]

    return fetch_json


def test_verified_main_commit_requires_one_successful_merged_pull_request() -> None:
    verified = PROVENANCE.verify_main_release_provenance(
        repository=REPOSITORY,
        release_sha=RELEASE_SHA,
        fetch_json=_fetcher(_success_responses()),
    )

    assert verified.pull_request_number == 108
    assert verified.pull_request_head_sha == PR_HEAD_SHA
    assert verified.ci_run_id == 12345


def test_public_repository_does_not_require_skipped_pr_image_build() -> None:
    responses = _success_responses()
    responses[f"/repos/{REPOSITORY}"] = {"private": False}
    responses[f"/repos/{REPOSITORY}/actions/runs/12345/jobs?per_page=100"] = {
        "jobs": [
            _successful_item(name=name, timestamp="2026-07-27T08:48:00Z")
            for name in sorted(PROVENANCE.PUBLIC_REQUIRED_CI_JOB_NAMES)
        ]
    }

    verified = PROVENANCE.verify_main_release_provenance(
        repository=REPOSITORY,
        release_sha=RELEASE_SHA,
        fetch_json=_fetcher(responses),
    )

    assert verified.ci_run_id == 12345


def test_private_repository_still_requires_pr_image_build() -> None:
    responses = _success_responses()
    responses[f"/repos/{REPOSITORY}/actions/runs/12345/jobs?per_page=100"] = {
        "jobs": [
            _successful_item(name=name, timestamp="2026-07-27T08:48:00Z")
            for name in sorted(PROVENANCE.PUBLIC_REQUIRED_CI_JOB_NAMES)
        ]
    }

    with pytest.raises(PROVENANCE.ProvenanceError, match="Production image builds"):
        PROVENANCE.verify_main_release_provenance(
            repository=REPOSITORY,
            release_sha=RELEASE_SHA,
            fetch_json=_fetcher(responses),
        )


def test_direct_main_push_is_rejected_before_images_or_deployment() -> None:
    responses = _success_responses()
    responses[f"/repos/{REPOSITORY}/commits/{RELEASE_SHA}/pulls?per_page=100"] = []

    with pytest.raises(PROVENANCE.ProvenanceError, match="associated merged pull request"):
        PROVENANCE.verify_main_release_provenance(
            repository=REPOSITORY,
            release_sha=RELEASE_SHA,
            fetch_json=_fetcher(responses),
        )


def test_tree_change_after_pr_ci_is_rejected() -> None:
    responses = _success_responses()
    responses[f"/repos/{REPOSITORY}/git/commits/{RELEASE_SHA}"] = _commit("d" * 40)

    with pytest.raises(PROVENANCE.ProvenanceError, match="tree differs"):
        PROVENANCE.verify_main_release_provenance(
            repository=REPOSITORY,
            release_sha=RELEASE_SHA,
            fetch_json=_fetcher(responses),
        )


def test_main_commit_with_a_non_base_parent_explains_how_to_recover() -> None:
    responses = _success_responses()
    responses[f"/repos/{REPOSITORY}/git/commits/{RELEASE_SHA}"] = _commit(
        TREE_SHA,
        parent_sha="e" * 40,
    )

    with pytest.raises(
        PROVENANCE.ProvenanceError,
        match="update or rebase the PR onto the latest main",
    ):
        PROVENANCE.verify_main_release_provenance(
            repository=REPOSITORY,
            release_sha=RELEASE_SHA,
            fetch_json=_fetcher(responses),
        )


def test_latest_failed_text_encoding_workflow_blocks_release() -> None:
    responses = _success_responses()
    responses[
        (
            f"/repos/{REPOSITORY}/actions/workflows/{PROVENANCE.TEXT_ENCODING_WORKFLOW_FILE}"
            f"/runs?event=pull_request&head_sha={PR_HEAD_SHA}&per_page=100"
        )
    ] = {
        "workflow_runs": [
            _successful_workflow_run(run_id=54320, timestamp="2026-07-27T08:48:00Z"),
            {
                "id": 54322,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "failure",
                "completed_at": "2026-07-27T08:49:00Z",
            },
        ]
    }

    with pytest.raises(PROVENANCE.ProvenanceError, match="text encoding workflow"):
        PROVENANCE.verify_main_release_provenance(
            repository=REPOSITORY,
            release_sha=RELEASE_SHA,
            fetch_json=_fetcher(responses),
        )


def test_manual_text_check_cannot_mask_a_failed_pull_request_check() -> None:
    responses = _success_responses()
    text_workflow_runs_path = (
        f"/repos/{REPOSITORY}/actions/workflows/{PROVENANCE.TEXT_ENCODING_WORKFLOW_FILE}"
        f"/runs?event=pull_request&head_sha={PR_HEAD_SHA}&per_page=100"
    )
    responses[text_workflow_runs_path] = {
        "workflow_runs": [
            {
                "id": 54322,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "failure",
                "completed_at": "2026-07-27T08:49:00Z",
            },
            {
                "id": 54323,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-07-27T08:50:00Z",
            },
        ]
    }

    with pytest.raises(PROVENANCE.ProvenanceError, match="text encoding workflow"):
        PROVENANCE.verify_main_release_provenance(
            repository=REPOSITORY,
            release_sha=RELEASE_SHA,
            fetch_json=_fetcher(responses),
        )
