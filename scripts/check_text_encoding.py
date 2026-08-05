"""Reject malformed UTF-8 and common mojibake before it reaches main.

The checker deliberately reads raw Git-tracked bytes rather than relying on a
shell's default decoding.  It also validates PR title/body and newly introduced
commit messages when GitHub Actions passes the event payload.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BINARY_SUFFIXES = frozenset({".docx", ".gif", ".ico", ".jpeg", ".jpg", ".pdf", ".png", ".webp", ".woff", ".woff2", ".zip"})
MOJIBAKE_RULES = (
    ("replacement_character", re.compile(r"\ufffd")),
    ("c1_control_character", re.compile(r"[\u0080-\u009f]")),
    # Common UTF-8 bytes rendered as Latin-1 or Windows-1252 and then saved.
    ("latin_single_byte_utf8_fragment", re.compile(r"[\u00c2-\u00f4](?:[\u0080-\u00bf]|[\u20ac\u201a\u0192\u201e\u2026\u2020\u2021\u02c6\u2030\u0160\u2039\u0152\u017d\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u02dc\u2122\u0161\u203a\u0153\u017e\u0178])")),
    # Common UTF-8 Chinese bytes rendered as GBK and then saved.
    ("gbk_utf8_fragment", re.compile(r"[\u4e00-\u9fff]\u20ac")),
    ("gbk_mojibake_marker", re.compile(r"[\u951f\u951b\u9225\u9286]")),
)
QUESTION_PLACEHOLDER = re.compile(r"\?{2,}")


@dataclass(frozen=True)
class IntegrityIssue:
    location: str
    reason: str


def mojibake_reasons(text: str) -> list[str]:
    """Return high-signal mojibake markers without printing source content."""

    return [name for name, pattern in MOJIBAKE_RULES if pattern.search(text)]


def message_reasons(text: str) -> list[str]:
    reasons = mojibake_reasons(text)
    if QUESTION_PLACEHOLDER.search(text):
        reasons.append("suspicious_question_placeholder")
    return reasons


def _run_git(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("git_command_failed:" + " ".join(arguments[:2]))
    return completed.stdout


def _tracked_paths(root: Path) -> Iterable[Path]:
    for encoded_path in _run_git(root, "ls-files", "-z").split(b"\0"):
        if encoded_path:
            yield root / encoded_path.decode("utf-8", errors="strict")


def _is_binary_path(path: Path) -> bool:
    return path.suffix.lower() in BINARY_SUFFIXES


def check_tracked_text(root: Path) -> list[IntegrityIssue]:
    issues: list[IntegrityIssue] = []
    for path in _tracked_paths(root):
        if _is_binary_path(path):
            continue
        data = path.read_bytes()
        relative = path.relative_to(root).as_posix()
        if b"\0" in data:
            issues.append(IntegrityIssue(relative, "unexpected_nul_byte"))
            continue
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            issues.append(IntegrityIssue(relative, "invalid_utf8"))
            continue
        for reason in mojibake_reasons(text):
            issues.append(IntegrityIssue(relative, reason))
    return issues


def _event_revision(payload: dict[str, Any]) -> str:
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        base = pull_request.get("base")
        head = pull_request.get("head")
        if isinstance(base, dict) and isinstance(head, dict):
            base_sha = str(base.get("sha") or "")
            head_sha = str(head.get("sha") or "")
            if re.fullmatch(r"[0-9a-f]{40}", base_sha) and re.fullmatch(r"[0-9a-f]{40}", head_sha):
                return f"{base_sha}..{head_sha}"
    before = str(payload.get("before") or "")
    after = str(payload.get("after") or "")
    if re.fullmatch(r"[0-9a-f]{40}", before) and before != "0" * 40 and re.fullmatch(r"[0-9a-f]{40}", after):
        return f"{before}..{after}"
    return "HEAD"


def check_event_metadata(event_path: Path) -> tuple[list[IntegrityIssue], str]:
    try:
        payload = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("github_event_unreadable") from error
    if not isinstance(payload, dict):
        raise RuntimeError("github_event_invalid")

    issues: list[IntegrityIssue] = []
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        for field in ("title", "body"):
            value = pull_request.get(field)
            if not isinstance(value, str):
                continue
            for reason in message_reasons(value):
                issues.append(IntegrityIssue(f"pull_request.{field}", reason))
    return issues, _event_revision(payload)


def check_commit_messages(root: Path, revision: str) -> list[IntegrityIssue]:
    try:
        arguments = ["-c", "i18n.logOutputEncoding=UTF-8", "log", "--format=%H%x00%B%x00"]
        if ".." in revision:
            arguments.append(revision)
        else:
            # A local/manual run should validate the current change, not fail
            # on an already merged historical commit that cannot be rewritten.
            arguments.extend(("-1", revision))
        raw = _run_git(root, *arguments)
        text = raw.decode("utf-8", errors="strict")
    except (RuntimeError, UnicodeDecodeError):
        return [IntegrityIssue("git_commit_messages", "unreadable_utf8")]

    issues: list[IntegrityIssue] = []
    parts = text.split("\0")
    for index in range(0, len(parts) - 1, 2):
        commit = parts[index].strip()
        message = parts[index + 1]
        if not commit:
            continue
        for reason in message_reasons(message):
            issues.append(IntegrityIssue(f"commit:{commit[:12]}", reason))
    return issues


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate repository and PR text encoding integrity.")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="Repository root to inspect.")
    parser.add_argument("--github-event", type=Path, help="Optional GitHub Actions event JSON payload.")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    root = arguments.root.resolve()
    issues = check_tracked_text(root)
    revision = "HEAD"
    if arguments.github_event:
        event_issues, revision = check_event_metadata(arguments.github_event)
        issues.extend(event_issues)
    issues.extend(check_commit_messages(root, revision))

    if issues:
        for issue in issues:
            print(f"text-encoding: {issue.location}: {issue.reason}")
        return 1
    print("text-encoding: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
