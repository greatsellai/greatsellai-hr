from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import (
    MailboxBackgroundJob,
    MailboxConfig,
    Organization,
    OrganizationMembership,
)
from app.services import mailbox_import_service, recruiting_agent_service
from app.services.identity_service import DEVELOPMENT_MEMBERSHIP_ID
from app.tenant_scope import bypass_organization_scope, set_organization_context


class _MailboxBindingImap:
    """Small deterministic IMAP double used only while binding test channels."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def login(self, *_args, **_kwargs) -> tuple[str, list[bytes]]:
        return "OK", [b"logged in"]

    def status(self, *_args, **_kwargs) -> tuple[str, list[bytes]]:
        return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 42)"]

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b"logged out"]


def _create_mailbox(
    client: TestClient,
    monkeypatch,
    *,
    label: str,
    host: str,
    email_address: str,
) -> dict[str, object]:
    monkeypatch.setattr(
        mailbox_import_service.imaplib,
        "IMAP4_SSL",
        _MailboxBindingImap,
    )
    response = client.post(
        "/v1/mailboxes",
        json={
            "display_name": label,
            "imap_host": host,
            "imap_port": 993,
            "email_address": email_address,
            "mailbox": "INBOX",
            "password": "test-agent-mailbox-authorization",
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "message",
    [
        "查看所有邮箱同步状态",
        "为什么所有邮箱没有同步？",
        "不要刷新所有邮箱",
        "不用拉取全部收件箱",
        "请停止同步所有收件邮箱",
        "取消收取全部邮箱",
        "查询全部收件邮箱的同步进度",
        "同步所有邮箱的状态",
        "所有邮箱同步了吗",
        "同步所有邮箱失败了",
        "同步所有邮箱报错了",
        "同步所有邮箱出错了",
        "同步所有邮箱异常",
        "sync failed all mailboxes",
        "sync all mailboxes failure",
        "sync all mailboxes errors",
        "sync all mailboxes issue",
        "同步所有邮箱已完成",
        "同步所有邮箱需要多久",
        "同步所有邮箱不行",
        "同步所有邮箱超时",
        "同步所有邮箱正在进行",
        "同步所有邮箱卡住",
        "同步所有邮箱没反应",
        "同步所有邮箱，可以吗？",
        "同步所有邮箱，行不行",
        "同步所有邮箱，你觉得呢？",
        "同步所有邮箱，算了",
        "同步所有邮箱，取消",
        "同步所有邮箱，刚才失败了",
        "同步所有邮箱，怎么回事？",
        "sync all mailboxes, never mind",
        "sync all mailboxes, cancel that",
    ],
)
def test_all_mailbox_sync_authorization_rejects_non_command_language(
    message: str,
) -> None:
    assert not recruiting_agent_service._explicitly_requests_all_mailbox_sync(message)


@pytest.mark.parametrize(
    "message",
    [
        "同步所有邮箱，除了测试邮箱",
        "同步所有邮箱，测试邮箱除外",
        "同步所有邮箱，不含测试邮箱",
        "同步所有邮箱，排除测试邮箱",
        "sync all mailboxes except test mailbox",
        "sync all mailboxes unless it is the test mailbox",
        "only sync all mailboxes",
        "同步所有邮箱，不同步测试邮箱",
        "sync all mailboxes, do not sync the test mailbox",
        "只同步所有邮箱",
    ],
)
def test_all_mailbox_sync_authorization_rejects_exclusions(message: str) -> None:
    assert not recruiting_agent_service._explicitly_requests_all_mailbox_sync(message)


@pytest.mark.parametrize(
    "message",
    [
        "查看校招邮箱的同步状态",
        "为什么校招邮箱没有同步？",
        "不要刷新校招邮箱",
        "不用拉取校招邮箱",
        "请停止同步校招邮箱",
        "取消收取校招邮箱",
        "查询校招邮箱同步进度",
        "校招邮箱同步了吗",
        "同步校招邮箱失败了",
        "同步校招邮箱报错了",
        "sync 校招邮箱 failed",
        "同步校招邮箱已完成",
        "同步校招邮箱需要多久",
        "同步校招邮箱不行",
        "同步校招邮箱超时",
        "同步校招邮箱正在进行",
        "同步校招邮箱卡住",
        "同步校招邮箱没反应",
        "同步校招邮箱，可以吗？",
        "同步校招邮箱，行不行",
        "同步校招邮箱，你觉得呢？",
        "同步校招邮箱，算了",
        "同步校招邮箱，取消",
        "同步校招邮箱，刚才失败了",
        "同步校招邮箱，怎么回事？",
        "sync 校招邮箱, never mind",
        "sync 校招邮箱, cancel that",
    ],
)
def test_named_mailbox_sync_authorization_rejects_non_command_language(
    message: str,
) -> None:
    assert not recruiting_agent_service._explicitly_requests_named_mailbox_sync(
        message,
        "校招邮箱",
    )


@pytest.mark.parametrize(
    "message",
    [
        "同步全部收件邮箱",
        "请同步所有收件邮箱",
        "请帮我刷新全部收件邮箱",
        "拉取所有收件箱",
        "please sync all mailboxes",
        "can you sync all mailboxes?",
        "同步所有邮箱，完成后通知我",
    ],
)
def test_all_mailbox_sync_authorization_accepts_explicit_commands(
    message: str,
) -> None:
    assert recruiting_agent_service._explicitly_requests_all_mailbox_sync(message)


@pytest.mark.parametrize(
    "message",
    [
        "同步校招邮箱",
        "请帮我刷新校招邮箱",
        "请拉取校招邮箱",
        "麻烦收取校招邮箱",
        "please sync 校招邮箱",
        "请把校招邮箱同步一下",
        "重新同步校招邮箱",
        "先同步校招邮箱",
        "只同步校招邮箱",
        "继续同步校招邮箱",
        "再同步校招邮箱",
        "同步校招邮箱，完成后告诉我结果",
        "同步校招邮箱，完成后通知我",
        "could you sync 校招邮箱?",
        "would you please refresh 校招邮箱？",
    ],
)
def test_named_mailbox_sync_authorization_accepts_explicit_commands(
    message: str,
) -> None:
    assert recruiting_agent_service._explicitly_requests_named_mailbox_sync(
        message,
        "校招邮箱",
    )


def test_named_mailbox_sync_authorization_binds_polarity_to_exact_target() -> None:
    message = "同步校招邮箱，不同步社招邮箱"

    assert not recruiting_agent_service._explicitly_requests_named_mailbox_sync(
        message,
        "校招邮箱",
    )
    assert not recruiting_agent_service._explicitly_requests_named_mailbox_sync(
        message,
        "社招邮箱",
    )
    assert not recruiting_agent_service._explicitly_requests_named_mailbox_sync(
        "同步社招邮箱，不同步社招邮箱",
        "社招邮箱",
    )
    assert not recruiting_agent_service._explicitly_requests_named_mailbox_sync(
        "同步社招邮箱，社招邮箱同步取消",
        "社招邮箱",
    )
    assert not recruiting_agent_service._explicitly_requests_named_mailbox_sync(
        "同步社招邮箱，could you not sync 社招邮箱?",
        "社招邮箱",
    )


def test_named_mailbox_sync_authorization_does_not_guess_overlapping_name() -> None:
    message = "同步校招邮箱吧"

    assert not recruiting_agent_service._explicitly_requests_named_mailbox_sync(
        message,
        "校招邮箱",
    )
    assert recruiting_agent_service._explicitly_requests_named_mailbox_sync(
        message,
        "校招邮箱吧",
    )


@pytest.mark.parametrize(
    ("message", "short_name", "long_name"),
    [
        ("同步校招邮箱！", "校招邮箱", "校招邮箱！"),
        ("sync campus mailbox.", "campus mailbox", "campus mailbox."),
    ],
)
def test_mailbox_literal_target_resolution_prefers_unique_longest_name(
    message: str,
    short_name: str,
    long_name: str,
) -> None:
    short = SimpleNamespace(mailbox_id="short", display_name=short_name)
    long = SimpleNamespace(mailbox_id="long", display_name=long_name)
    configs = [short, long]

    assert recruiting_agent_service._mailbox_configs_named_in_message(
        message,
        configs,
    ) == [long, short]
    assert recruiting_agent_service._unique_longest_mailbox_literal_match(
        message,
        configs,
    ) is long
    assert recruiting_agent_service._explicitly_requests_disambiguated_named_mailbox_sync(
        message,
        long_name,
    )


def test_mailbox_literal_target_resolution_rejects_equal_length_tie() -> None:
    campus = SimpleNamespace(mailbox_id="campus", display_name="校招邮箱")
    social = SimpleNamespace(mailbox_id="social", display_name="社招邮箱")

    assert recruiting_agent_service._unique_longest_mailbox_literal_match(
        "同步校招邮箱和社招邮箱",
        [campus, social],
    ) is None


@pytest.mark.parametrize("mailbox_name", ["历史邮箱", "状态邮箱", "异常邮箱"])
def test_named_mailbox_sync_authorization_does_not_reject_words_inside_name(
    mailbox_name: str,
) -> None:
    assert recruiting_agent_service._explicitly_requests_named_mailbox_sync(
        f"同步{mailbox_name}",
        mailbox_name,
    )


def test_agent_queries_mailbox_state_without_receiving_connection_or_attachment_data(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    mailbox = _create_mailbox(
        ai_client,
        monkeypatch,
        label="算法社招",
        host="imap.agent-status.test",
        email_address="algorithm-recruiting@example.test",
    )
    mailbox_id = str(mailbox["mailbox_id"])
    with ai_client.app.state.database.session_factory() as session:
        config = session.get(MailboxConfig, mailbox_id)
        assert config is not None
        set_organization_context(session, config.organization_id)
        mailbox_import_service._record(
            session,
            config=config,
            uid="42",
            message_id="<agent-status@example.test>",
            filename="private-candidate-resume.pdf",
            attachment_sha256="a" * 64,
            status="failed",
            error="unsupported_document_type",
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()

    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            tool_names = {
                item["function"]["name"]
                for item in recruiting_agent_service._ACTIVE_TOOL_DEFINITIONS.get()
            }
            assert {"get_mailbox_status", "get_recent_mailbox_imports"}.issubset(tool_names)
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "mailbox-status",
                        "type": "function",
                        "function": {
                            "name": "get_mailbox_status",
                            "arguments": json.dumps({"mailbox_name": "算法社招"}),
                        },
                    }
                ],
            }
        tool_payload = messages[-1]["content"]
        assert "算法社招" in tool_payload
        assert "imap.agent-status.test" not in tool_payload
        assert "algorithm-recruiting@example.test" not in tool_payload
        assert "test-agent-mailbox-authorization" not in tool_payload
        assert "private-candidate-resume.pdf" not in tool_payload
        return {"content": "## 邮箱状态\n\n已读取算法社招的同步状态和最近失败汇总。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "查看算法社招邮箱的状态和失败附件情况"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["intent"] == "show_mailbox_status"
    assert payload["tool_trace"] == [
        {"tool": "收件邮箱状态", "summary": "已读取 1 个收件通道的安全状态。"}
    ]
    assert payload["actions"] == [
        {
            "action": "open_mailbox_workspace",
            "label": "打开邮箱附件入库",
            "resume_id": None,
        }
    ]


def test_agent_queues_named_mailbox_sync_without_opening_imap(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="校招邮箱",
        host="imap.agent-sync.test",
        email_address="campus-recruiting@example.test",
    )

    def unexpected_imap(*_args, **_kwargs):
        raise AssertionError("Agent sync must only enqueue a durable job")

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", unexpected_imap)
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings, messages
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "sync-campus",
                        "type": "function",
                        "function": {
                            "name": "enqueue_named_mailbox_sync",
                            "arguments": json.dumps({"mailbox_name": "校招邮箱"}),
                        },
                    }
                ],
            }
        return {"content": "已将校招邮箱加入后台同步队列，尚未宣称同步完成。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "同步校招邮箱"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["intent"] == "sync_mailbox"
    assert payload["tool_trace"] == [
        {"tool": "收件邮箱同步", "summary": "已为“校招邮箱”创建后台同步任务。"}
    ]
    tasks = ai_client.get("/v1/mailbox/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["total"] == 1
    assert tasks.json()["items"][0]["status"] == "queued"
    assert tasks.json()["items"][0]["job_kind"] == "sync"


def test_agent_rejects_shorter_mailbox_tool_target_when_longer_name_matches(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="校招邮箱",
        host="imap.agent-short-target.test",
        email_address="short-target@example.test",
    )
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="校招邮箱！",
        host="imap.agent-long-target.test",
        email_address="long-target@example.test",
    )
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "wrong-short-target",
                        "type": "function",
                        "function": {
                            "name": "enqueue_named_mailbox_sync",
                            "arguments": json.dumps({"mailbox_name": "校招邮箱"}),
                        },
                    }
                ],
            }
        assert "唯一收件通道名称" in messages[-1]["content"]
        return {"content": "目标名称存在重叠，我没有创建同步任务。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "同步校招邮箱！"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["tool_trace"] == [
        {
            "tool": "收件邮箱同步",
            "summary": "请明确复述要同步的唯一收件通道名称，未创建同步任务。",
        }
    ]
    tasks = ai_client.get("/v1/mailbox/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["total"] == 0


def test_agent_rejects_sync_all_when_message_matches_a_config_name(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="所有邮箱！",
        host="imap.agent-all-name-collision.test",
        email_address="all-name-collision@example.test",
    )
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "ambiguous-all-target",
                        "type": "function",
                        "function": {
                            "name": "enqueue_all_mailbox_syncs",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        assert "全部邮箱指令有歧义" in messages[-1]["content"]
        return {"content": "名称与全量指令有歧义，我没有创建同步任务。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "同步所有邮箱！"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["tool_trace"] == [
        {
            "tool": "全部收件邮箱同步",
            "summary": (
                "检测到收件通道名称与全部邮箱指令有歧义，"
                "请明确复述要同步全部邮箱还是指定邮箱，未创建同步任务。"
            ),
        }
    ]
    tasks = ai_client.get("/v1/mailbox/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["total"] == 0


def test_agent_rejects_named_sync_when_config_name_looks_like_sync_all(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="所有邮箱！",
        host="imap.agent-named-all-collision.test",
        email_address="named-all-collision@example.test",
    )
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "ambiguous-named-all-target",
                        "type": "function",
                        "function": {
                            "name": "enqueue_named_mailbox_sync",
                            "arguments": json.dumps({"mailbox_name": "所有邮箱！"}),
                        },
                    }
                ],
            }
        assert "全部邮箱指令有歧义" in messages[-1]["content"]
        return {"content": "名称同时像全量指令，我没有创建同步任务。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "同步所有邮箱！"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["tool_trace"] == [
        {
            "tool": "收件邮箱同步",
            "summary": (
                "检测到指定收件通道名称与全部邮箱指令有歧义，"
                "请明确复述要同步全部邮箱还是指定邮箱，未创建同步任务。"
            ),
        }
    ]
    tasks = ai_client.get("/v1/mailbox/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["total"] == 0


@pytest.mark.parametrize(
    ("case_id", "archived_name", "active_name", "message", "tool_name"),
    [
        (
            "zh-named",
            "所有邮箱！",
            "日常招聘",
            "同步所有邮箱！",
            "enqueue_named_mailbox_sync",
        ),
        (
            "zh-all",
            "所有邮箱！",
            "日常招聘",
            "同步所有邮箱！",
            "enqueue_all_mailbox_syncs",
        ),
        (
            "en-named",
            "all mailboxes.",
            "daily recruiting",
            "sync all mailboxes.",
            "enqueue_named_mailbox_sync",
        ),
        (
            "en-all",
            "all mailboxes.",
            "daily recruiting",
            "sync all mailboxes.",
            "enqueue_all_mailbox_syncs",
        ),
    ],
)
def test_agent_archived_all_like_name_blocks_named_and_all_syncs(
    ai_client: TestClient,
    monkeypatch,
    *,
    case_id: str,
    archived_name: str,
    active_name: str,
    message: str,
    tool_name: str,
) -> None:
    archived = _create_mailbox(
        ai_client,
        monkeypatch,
        label=archived_name,
        host=f"imap.agent-archived-{case_id}.test",
        email_address=f"archived-{case_id}@example.test",
    )
    active = _create_mailbox(
        ai_client,
        monkeypatch,
        label=active_name,
        host=f"imap.agent-active-{case_id}.test",
        email_address=f"active-{case_id}@example.test",
    )
    archive = ai_client.post(f"/v1/mailboxes/{archived['mailbox_id']}/archive")
    assert archive.status_code == 200, archive.text
    assert archive.json()["archived_at"] is not None
    assert archive.json()["enabled"] is False

    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            arguments = (
                json.dumps({"mailbox_name": archived_name})
                if tool_name == "enqueue_named_mailbox_sync"
                else "{}"
            )
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": f"archived-all-like-{case_id}",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": arguments},
                    }
                ],
            }
        expected = (
            "已归档"
            if tool_name == "enqueue_named_mailbox_sync"
            else "全部邮箱指令有歧义"
        )
        assert expected in messages[-1]["content"]
        return {"content": "归档名称仍参与鉴权，我没有创建同步任务。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": message},
    )

    assert response.status_code == 200, response.text
    trace = response.json()["tool_trace"]
    assert len(trace) == 1
    if tool_name == "enqueue_named_mailbox_sync":
        assert trace[0] == {
            "tool": "收件邮箱同步",
            "summary": "该收件通道已归档，未创建同步任务。",
        }
    else:
        assert trace[0] == {
            "tool": "全部收件邮箱同步",
            "summary": (
                "检测到收件通道名称与全部邮箱指令有歧义，"
                "请明确复述要同步全部邮箱还是指定邮箱，未创建同步任务。"
            ),
        }
    tasks = ai_client.get("/v1/mailbox/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["total"] == 0
    active_configs = ai_client.get("/v1/mailboxes")
    assert active_configs.status_code == 200, active_configs.text
    assert [item["mailbox_id"] for item in active_configs.json()["items"]] == [
        active["mailbox_id"]
    ]


def test_agent_queries_recent_import_aggregate_without_attachment_metadata(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    mailbox = _create_mailbox(
        ai_client,
        monkeypatch,
        label="产品收件箱",
        host="imap.agent-imports.test",
        email_address="product-recruiting@example.test",
    )
    with ai_client.app.state.database.session_factory() as session:
        config = session.get(MailboxConfig, str(mailbox["mailbox_id"]))
        assert config is not None
        set_organization_context(session, config.organization_id)
        mailbox_import_service._record(
            session,
            config=config,
            uid="43",
            message_id="<agent-imports@example.test>",
            filename="do-not-show-this-filename.docx",
            attachment_sha256="b" * 64,
            status="failed",
            error="unsupported_document_type",
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()

    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "mailbox-imports",
                        "type": "function",
                        "function": {
                            "name": "get_recent_mailbox_imports",
                            "arguments": json.dumps({"mailbox_name": "产品收件箱"}),
                        },
                    }
                ],
            }
        tool_payload = messages[-1]["content"]
        assert "附件格式不受支持" in tool_payload
        assert "do-not-show-this-filename.docx" not in tool_payload
        assert "product-recruiting@example.test" not in tool_payload
        return {"content": "产品收件箱最近有 1 份格式不支持的附件，未展示附件名称。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "查看产品收件箱最近附件入库情况"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["intent"] == "show_mailbox_imports"
    assert response.json()["tool_trace"] == [
        {"tool": "附件入库状态", "summary": "已读取最近附件入库的汇总状态。"}
    ]


def test_agent_syncs_all_channels_only_through_independent_background_jobs(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="社招邮箱",
        host="imap.agent-all-social.test",
        email_address="social-recruiting@example.test",
    )
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="猎头邮箱",
        host="imap.agent-all-hunter.test",
        email_address="hunter-recruiting@example.test",
    )

    def unexpected_imap(*_args, **_kwargs):
        raise AssertionError("Agent sync-all must only enqueue durable jobs")

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", unexpected_imap)
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings, messages
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "sync-all",
                        "type": "function",
                        "function": {
                            "name": "enqueue_all_mailbox_syncs",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        return {"content": "已为全部启用收件邮箱创建独立后台同步任务。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "同步所有收件邮箱"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["intent"] == "sync_mailbox"
    assert response.json()["tool_trace"] == [
        {"tool": "全部收件邮箱同步", "summary": "已为 2 个收件通道创建后台同步任务。"}
    ]
    tasks = ai_client.get("/v1/mailbox/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["total"] == 2
    assert {item["status"] for item in tasks.json()["items"]} == {"queued"}


def test_agent_does_not_sync_every_inbox_when_request_is_ambiguous(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="校招邮箱",
        host="imap.agent-ambiguous-campus.test",
        email_address="campus-ambiguous@example.test",
    )
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="社招邮箱",
        host="imap.agent-ambiguous-social.test",
        email_address="social-ambiguous@example.test",
    )
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            # A model can occasionally over-eagerly call the all-channel tool.
            # The execution boundary must still refuse a vague user request.
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "ambiguous-sync-all",
                        "type": "function",
                        "function": {
                            "name": "enqueue_all_mailbox_syncs",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        assert "请明确说明要同步全部收件邮箱" in messages[-1]["content"]
        return {"content": "请说明要同步校招邮箱还是社招邮箱；我没有创建全量同步任务。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "同步邮箱"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["intent"] == "sync_mailbox"
    assert response.json()["tool_trace"] == [
        {
            "tool": "全部收件邮箱同步",
            "summary": "请明确说明要同步全部收件邮箱，未创建同步任务。",
        }
    ]
    tasks = ai_client.get("/v1/mailbox/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["total"] == 0


def test_agent_does_not_sync_a_named_inbox_when_user_did_not_name_it(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="校招邮箱",
        host="imap.agent-named-authorization.test",
        email_address="campus-named-authorization@example.test",
    )
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            # The model knows the channel name from context, but the user did
            # not select it. Context must never become sync authorization.
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "guessed-named-sync",
                        "type": "function",
                        "function": {
                            "name": "enqueue_named_mailbox_sync",
                            "arguments": json.dumps({"mailbox_name": "校招邮箱"}),
                        },
                    }
                ],
            }
        assert "请明确指定要同步的收件通道" in messages[-1]["content"]
        return {"content": "请明确说出要同步的收件通道名称；我没有创建同步任务。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "同步邮箱"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["tool_trace"] == [
        {
            "tool": "收件邮箱同步",
            "summary": "请明确指定要同步的收件通道，未创建同步任务。",
        }
    ]
    tasks = ai_client.get("/v1/mailbox/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["total"] == 0


def test_agent_does_not_treat_all_failed_attachments_as_all_mailbox_sync(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="附件失败测试",
        host="imap.agent-attachment-authorization.test",
        email_address="attachment-authorization@example.test",
    )
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "attachment-is-not-all-inboxes",
                        "type": "function",
                        "function": {
                            "name": "enqueue_all_mailbox_syncs",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        assert "请明确说明要同步全部收件邮箱" in messages[-1]["content"]
        return {"content": "“所有失败附件”不是全部收件邮箱同步，我没有创建同步任务。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "同步所有失败附件"},
    )

    assert response.status_code == 200, response.text
    tasks = ai_client.get("/v1/mailbox/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["total"] == 0


def test_agent_cannot_query_or_sync_a_mailbox_from_another_workspace(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """The Agent receives only the request-bound workspace's mailbox scope."""

    database = ai_client.app.state.database
    with database.session_factory() as session:
        foreign_organization = Organization(name="Agent private workspace")
        session.add(foreign_organization)
        session.flush()
        with bypass_organization_scope(session):
            foreign_mailbox = MailboxConfig(
                organization_id=foreign_organization.id,
                display_name="外部私有收件箱",
                display_name_key="外部私有收件箱",
                imap_host="imap.private-workspace.test",
                imap_port=993,
                email_address="private-workspace@example.test",
                mailbox="INBOX",
                encrypted_password="not-a-real-secret",
                enabled=True,
            )
            session.add(foreign_mailbox)
            session.flush()
        foreign_mailbox_id = foreign_mailbox.id
        session.commit()

    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            # This is a deliberately malicious tool call: the channel is not
            # present in the Agent context for the legacy/current workspace.
            context = messages[-1]["content"]
            assert "外部私有收件箱" not in context
            assert "imap.private-workspace.test" not in context
            assert "private-workspace@example.test" not in context
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "cross-workspace-sync",
                        "type": "function",
                        "function": {
                            "name": "enqueue_named_mailbox_sync",
                            "arguments": json.dumps({"mailbox_name": "外部私有收件箱"}),
                        },
                    }
                ],
            }
        assert "未找到该收件通道" in messages[-1]["content"]
        assert "外部私有收件箱" not in messages[-1]["content"]
        assert "imap.private-workspace.test" not in messages[-1]["content"]
        assert "private-workspace@example.test" not in messages[-1]["content"]
        return {"content": "当前工作区没有这个收件通道，未创建同步任务。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "同步指定收件通道"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["intent"] == "sync_mailbox"
    assert response.json()["tool_trace"] == [
        {
            "tool": "收件邮箱同步",
            "summary": "未找到该收件通道，未创建同步任务。",
        }
    ]
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            assert session.scalar(
                select(func.count())
                .select_from(MailboxBackgroundJob)
                .where(MailboxBackgroundJob.mailbox_config_id == foreign_mailbox_id)
            ) == 0


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_summary"),
    [
        (
            "unknown_mailbox_operation",
            "{}",
            "请求的工具不可用，未执行任何操作。",
        ),
        (
            "enqueue_named_mailbox_sync",
            "{not-valid-json",
            "工具调用参数无法识别，未执行任何操作。",
        ),
    ],
)
def test_agent_recovers_from_unusable_model_tool_calls_without_a_500(
    ai_client: TestClient,
    monkeypatch,
    *,
    tool_name: str,
    arguments: str,
    expected_summary: str,
) -> None:
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "unusable-tool-call",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": arguments},
                    }
                ],
            }
        assert expected_summary in messages[-1]["content"]
        assert tool_name not in messages[-1]["content"]
        return {"content": "刚才的工具调用无效，未执行任何操作。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "查看邮箱状态"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["tool_trace"] == [
        {"tool": "Agent 工具", "summary": expected_summary}
    ]
    tasks = ai_client.get("/v1/mailbox/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["total"] == 0


def test_agent_hides_and_hard_rejects_mailbox_tools_for_recruiter_role(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="招聘专员不可用邮箱",
        host="imap.agent-recruiter-role.test",
        email_address="recruiter-role@example.test",
    )
    with ai_client.app.state.database.session_factory() as session:
        membership = session.get(OrganizationMembership, DEVELOPMENT_MEMBERSHIP_ID)
        assert membership is not None
        membership.role = "recruiter"
        session.commit()

    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            tool_names = {
                item["function"]["name"]
                for item in recruiting_agent_service._ACTIVE_TOOL_DEFINITIONS.get()
            }
            assert "get_mailbox_status" not in tool_names
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "recruiter-mailbox-status",
                        "type": "function",
                        "function": {"name": "get_mailbox_status", "arguments": "{}"},
                    }
                ],
            }
        assert "没有管理收件邮箱的权限" in messages[-1]["content"]
        return {"content": "当前账号没有邮箱管理权限，未读取或同步邮箱。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "查看收件邮箱状态"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["tool_trace"] == [
        {
            "tool": "收件邮箱",
            "summary": "当前账号没有管理收件邮箱的权限或套餐未开通该功能，未读取或同步任何邮箱。",
        }
    ]
    tasks = ai_client.get("/v1/mailbox/tasks")
    assert tasks.status_code == 403, tasks.text


def test_agent_hides_and_hard_rejects_mailbox_tools_when_feature_is_unavailable(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    _create_mailbox(
        ai_client,
        monkeypatch,
        label="不可用测试邮箱",
        host="imap.agent-disabled.test",
        email_address="disabled-recruiting@example.test",
    )
    monkeypatch.setattr("app.main.require_feature", lambda _principal, _feature: False)
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings
        calls += 1
        if calls == 1:
            tool_names = {
                item["function"]["name"]
                for item in recruiting_agent_service._ACTIVE_TOOL_DEFINITIONS.get()
            }
            assert "get_mailbox_status" not in tool_names
            # A malformed or malicious model response must still not bypass
            # the server-side entitlement check.
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "disallowed-mailbox-status",
                        "type": "function",
                        "function": {
                            "name": "get_mailbox_status",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        assert "没有管理收件邮箱的权限" in messages[-1]["content"]
        return {"content": "当前账号没有邮箱管理权限，未读取或同步邮箱。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "查看收件邮箱状态"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["intent"] == "show_mailbox_status"
    assert response.json()["tool_trace"] == [
        {
            "tool": "收件邮箱",
            "summary": "当前账号没有管理收件邮箱的权限或套餐未开通该功能，未读取或同步任何邮箱。",
        }
    ]
    # The same entitlement also blocks the direct mailbox endpoint. Verify
    # the Agent did not create a job through a side channel instead.
    tasks = ai_client.get("/v1/mailbox/tasks")
    assert tasks.status_code == 403, tasks.text
    with ai_client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MailboxBackgroundJob)) == 0
