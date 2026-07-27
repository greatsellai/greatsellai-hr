#!/usr/bin/env python3
"""Refuse a main release that is not backed by a fully verified merged PR.

The repository currently uses squash merges, so the commit created on ``main``
has a different SHA from the pull request head that CI tested.  This guard
links the two through GitHub's "pull requests associated with a commit" API,
then verifies that the merged PR's source tree and required PR checks match
the commit that is about to be released.

It deliberately uses only GitHub API metadata and never writes to GitHub or
production.  A direct push to ``main`` therefore fails before production
images are built or the deployment workflow can run.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

REQUIRED_CI_JOB_NAMES = frozenset(
    {
        "Backend tests",
        "PostgreSQL mailbox race",
        "Web build",
        "Production image builds",
    }
)
REQUIRED_TEXT_CHECK_NAME = "UTF-8 source and PR metadata"
TEXT_ENCODING_WORKFLOW_FILE = "text-encoding.yml"


class ProvenanceError(RuntimeError):
    """The candidate main commit cannot safely be released."""


JsonFetcher = Callable[[str], Any]


def _require_mapping(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProvenanceError(f"invalid GitHub API response for {context}")
    return value


def _require_sequence(value: Any, *, context: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ProvenanceError(f"invalid GitHub API response for {context}")
    return value


def _successful_completed(item: Mapping[str, Any]) -> bool:
    return item.get("status") == "completed" and item.get("conclusion") == "success"


def _latest_item(items: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not items:
        raise ProvenanceError("required GitHub check is missing")
    return max(
        items,
        key=lambda item: str(
            item.get("completed_at") or item.get("started_at") or item.get("created_at") or ""
        ),
    )


def _tree_sha(commit: Mapping[str, Any], *, context: str) -> str:
    # ``GET /git/commits/{sha}`` returns ``tree`` at the top level.  Accept
    # the nested form too so this validator remains compatible with a commit
    # object returned by GitHub's non-git commit endpoint.
    tree_value = commit.get("tree")
    if not isinstance(tree_value, Mapping):
        commit_data = _require_mapping(commit.get("commit"), context=f"{context}.commit")
        tree_value = commit_data.get("tree")
    tree_data = _require_mapping(tree_value, context=f"{context}.tree")
    tree_sha = tree_data.get("sha")
    if not isinstance(tree_sha, str) or not SHA_PATTERN.fullmatch(tree_sha):
        raise ProvenanceError(f"invalid tree SHA for {context}")
    return tree_sha


def _single_parent_sha(commit: Mapping[str, Any]) -> str:
    parents = _require_sequence(commit.get("parents"), context="main release commit parents")
    if len(parents) != 1:
        raise ProvenanceError(
            "main release commit must have exactly one parent from the PR base"
        )
    parent = _require_mapping(parents[0], context="main release commit parent")
    parent_sha = parent.get("sha")
    if not isinstance(parent_sha, str) or not SHA_PATTERN.fullmatch(parent_sha):
        raise ProvenanceError("main release commit has an invalid parent SHA")
    return parent_sha


def _associated_merged_pull_request(
    *, repository: str, release_sha: str, fetch_json: JsonFetcher
) -> Mapping[str, Any]:
    pull_requests = _require_sequence(
        fetch_json(f"/repos/{repository}/commits/{release_sha}/pulls?per_page=100"),
        context="associated pull requests",
    )
    candidates: list[Mapping[str, Any]] = []
    for value in pull_requests:
        pull_request = _require_mapping(value, context="associated pull request")
        if (
            pull_request.get("state") == "closed"
            and pull_request.get("merged_at")
            and pull_request.get("merge_commit_sha") == release_sha
        ):
            candidates.append(pull_request)

    if not candidates:
        raise ProvenanceError(
            "main commit is not the merge commit of an associated merged pull request"
        )
    if len(candidates) != 1:
        raise ProvenanceError("main commit is associated with multiple merged pull requests")
    return candidates[0]


def _verified_pr_head_sha(
    *, pull_request: Mapping[str, Any], repository: str
) -> str:
    head = _require_mapping(pull_request.get("head"), context="pull request head")
    head_repository = _require_mapping(head.get("repo"), context="pull request head repository")
    if head_repository.get("full_name") != repository:
        raise ProvenanceError("merged pull request head is not owned by this repository")
    head_sha = head.get("sha")
    if not isinstance(head_sha, str) or not SHA_PATTERN.fullmatch(head_sha):
        raise ProvenanceError("merged pull request has an invalid head SHA")
    return head_sha


def _verified_pr_base_sha(
    *, pull_request: Mapping[str, Any], repository: str
) -> str:
    base = _require_mapping(pull_request.get("base"), context="pull request base")
    base_repository = _require_mapping(base.get("repo"), context="pull request base repository")
    if base_repository.get("full_name") != repository or base.get("ref") != "main":
        raise ProvenanceError("merged pull request does not target this repository main branch")
    base_sha = base.get("sha")
    if not isinstance(base_sha, str) or not SHA_PATTERN.fullmatch(base_sha):
        raise ProvenanceError("merged pull request has an invalid base SHA")
    return base_sha


def _verify_full_ci(
    *, repository: str, head_sha: str, fetch_json: JsonFetcher
) -> int:
    workflow_runs = _require_mapping(
        fetch_json(
            f"/repos/{repository}/actions/workflows/ci.yml/runs?event=pull_request"
            f"&head_sha={head_sha}&per_page=100"
        ),
        context="pull request CI runs",
    )
    runs = _require_sequence(workflow_runs.get("workflow_runs"), context="pull request CI runs")
    matching_runs = [
        _require_mapping(value, context="pull request CI run")
        for value in runs
        if _require_mapping(value, context="pull request CI run").get("event") == "pull_request"
    ]
    latest_run = _latest_item(matching_runs)
    if not _successful_completed(latest_run):
        raise ProvenanceError("latest pull request CI run is not successful")

    run_id = latest_run.get("id")
    if not isinstance(run_id, int):
        raise ProvenanceError("latest pull request CI run has an invalid id")

    jobs_response = _require_mapping(
        fetch_json(f"/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"),
        context="pull request CI jobs",
    )
    jobs = _require_sequence(jobs_response.get("jobs"), context="pull request CI jobs")
    jobs_by_name: dict[str, list[Mapping[str, Any]]] = {}
    for value in jobs:
        job = _require_mapping(value, context="pull request CI job")
        name = job.get("name")
        if isinstance(name, str):
            jobs_by_name.setdefault(name, []).append(job)

    missing_or_failed = [
        name
        for name in sorted(REQUIRED_CI_JOB_NAMES)
        if name not in jobs_by_name or not _successful_completed(_latest_item(jobs_by_name[name]))
    ]
    if missing_or_failed:
        raise ProvenanceError(
            "required pull request CI jobs are not successful: "
            + ", ".join(missing_or_failed)
        )
    return run_id


def _verify_text_metadata_check(
    *, repository: str, head_sha: str, fetch_json: JsonFetcher
) -> None:
    workflow_runs_response = _require_mapping(
        fetch_json(
            f"/repos/{repository}/actions/workflows/{TEXT_ENCODING_WORKFLOW_FILE}/runs"
            f"?event=pull_request&head_sha={head_sha}&per_page=100"
        ),
        context="pull request text encoding workflow runs",
    )
    workflow_runs = _require_sequence(
        workflow_runs_response.get("workflow_runs"),
        context="pull request text encoding workflow runs",
    )
    matching_runs = [
        _require_mapping(value, context="pull request text encoding workflow run")
        for value in workflow_runs
        if _require_mapping(value, context="pull request text encoding workflow run").get("event")
        == "pull_request"
    ]
    latest_run = _latest_item(matching_runs)
    if not _successful_completed(latest_run):
        raise ProvenanceError("latest pull request text encoding workflow is not successful")

    run_id = latest_run.get("id")
    if not isinstance(run_id, int):
        raise ProvenanceError("latest pull request text encoding workflow has an invalid id")

    jobs_response = _require_mapping(
        fetch_json(f"/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"),
        context="pull request text encoding jobs",
    )
    jobs = _require_sequence(
        jobs_response.get("jobs"),
        context="pull request text encoding jobs",
    )
    matching_jobs = [
        _require_mapping(value, context="pull request text encoding job")
        for value in jobs
        if _require_mapping(value, context="pull request text encoding job").get("name")
        == REQUIRED_TEXT_CHECK_NAME
    ]
    latest_job = _latest_item(matching_jobs)
    if not _successful_completed(latest_job):
        raise ProvenanceError("latest pull request text metadata check is not successful")


@dataclass(frozen=True)
class VerifiedReleaseProvenance:
    pull_request_number: int
    pull_request_head_sha: str
    ci_run_id: int


def verify_main_release_provenance(
    *, repository: str, release_sha: str, fetch_json: JsonFetcher
) -> VerifiedReleaseProvenance:
    """Verify that ``release_sha`` is the fully tested tree from one merged PR."""

    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ProvenanceError("invalid GitHub repository name")
    if not SHA_PATTERN.fullmatch(release_sha):
        raise ProvenanceError("invalid release commit SHA")

    pull_request = _associated_merged_pull_request(
        repository=repository,
        release_sha=release_sha,
        fetch_json=fetch_json,
    )
    number = pull_request.get("number")
    if not isinstance(number, int):
        raise ProvenanceError("merged pull request has an invalid number")
    head_sha = _verified_pr_head_sha(pull_request=pull_request, repository=repository)
    base_sha = _verified_pr_base_sha(pull_request=pull_request, repository=repository)

    release_commit = _require_mapping(
        fetch_json(f"/repos/{repository}/git/commits/{release_sha}"),
        context="main release commit",
    )
    pull_request_commit = _require_mapping(
        fetch_json(f"/repos/{repository}/git/commits/{head_sha}"),
        context="pull request head commit",
    )
    if _single_parent_sha(release_commit) != base_sha:
        raise ProvenanceError(
            "main release commit is not a direct squash merge from the verified PR base"
        )
    if _tree_sha(release_commit, context="main release commit") != _tree_sha(
        pull_request_commit,
        context="pull request head commit",
    ):
        raise ProvenanceError("main commit tree differs from the merged pull request tree")

    ci_run_id = _verify_full_ci(
        repository=repository,
        head_sha=head_sha,
        fetch_json=fetch_json,
    )
    _verify_text_metadata_check(
        repository=repository,
        head_sha=head_sha,
        fetch_json=fetch_json,
    )
    return VerifiedReleaseProvenance(
        pull_request_number=number,
        pull_request_head_sha=head_sha,
        ci_run_id=ci_run_id,
    )


def _github_fetcher(*, api_url: str, token: str) -> JsonFetcher:
    normalized_api_url = api_url.rstrip("/")

    def fetch_json(path: str) -> Any:
        request = Request(
            f"{normalized_api_url}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "greatsellai-hr-release-provenance",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310 - GitHub URL is configured by Actions.
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise ProvenanceError(f"GitHub API request failed with HTTP {error.code}") from error
        except URLError as error:
            raise ProvenanceError("GitHub API request could not be completed") from error

    return fetch_json


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    release_sha = os.environ.get("GITHUB_SHA", "").lower()
    token = os.environ.get("GITHUB_TOKEN", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    if not token:
        print("release-provenance: missing GITHUB_TOKEN", file=sys.stderr)
        return 2

    try:
        verified = verify_main_release_provenance(
            repository=repository,
            release_sha=release_sha,
            fetch_json=_github_fetcher(api_url=api_url, token=token),
        )
    except ProvenanceError as error:
        print(f"release-provenance: failed: {error}", file=sys.stderr)
        return 1

    print(
        "release-provenance: passed "
        f"pr=#{verified.pull_request_number} "
        f"head={verified.pull_request_head_sha[:12]} "
        f"ci_run={verified.ci_run_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
