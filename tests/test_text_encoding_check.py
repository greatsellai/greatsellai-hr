from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_text_encoding.py"
SPEC = importlib.util.spec_from_file_location("check_text_encoding", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECKER
SPEC.loader.exec_module(CHECKER)


def test_valid_utf8_chinese_is_not_mojibake() -> None:
    text = "".join(chr(codepoint) for codepoint in (0x7B80, 0x5386, 0x7B5B, 0x9009))

    assert CHECKER.mojibake_reasons(text) == []


def test_common_latin1_and_gbk_mojibake_are_detected() -> None:
    latin1_saved_as_utf8 = "\u00e4\u00b8\u00ad"
    gbk_saved_as_utf8 = "\u7ee0\u20ac"

    assert "latin_single_byte_utf8_fragment" in CHECKER.mojibake_reasons(latin1_saved_as_utf8)
    assert "gbk_utf8_fragment" in CHECKER.mojibake_reasons(gbk_saved_as_utf8)


def test_replacement_character_and_lost_question_placeholder_are_detected() -> None:
    replacement_character = "prefix" + chr(0xFFFD) + "suffix"

    assert "replacement_character" in CHECKER.mojibake_reasons(replacement_character)
    assert "suspicious_question_placeholder" in CHECKER.message_reasons("**??? / ??**")


def test_pull_request_event_metadata_is_checked_without_printing_body(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "pull_request": {
                    "title": "safe title",
                    "body": "**??? / ??**",
                    "base": {"sha": "a" * 40},
                    "head": {"sha": "b" * 40},
                }
            }
        ),
        encoding="utf-8",
    )

    issues, revision = CHECKER.check_event_metadata(event_path)

    assert revision == f"{'a' * 40}..{'b' * 40}"
    assert {(issue.location, issue.reason) for issue in issues} == {
        ("pull_request.body", "suspicious_question_placeholder")
    }
