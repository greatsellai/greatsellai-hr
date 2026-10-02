"""Privacy regressions use synthetic fixtures, never customer records."""
import json

import pytest
from sqlalchemy import select

from app.integration_schemas import IntegrationGrantCreate, IntegrationPolicyPatch
from app.models import IntegrationAuditEvent, Resume, ResumeFactSnapshot, ResumeSourceBlock
from app.services.integration_management_service import create_integration_grant, update_integration_policy
from app.services.integration_read_service import (
    sanitize_integration_text,
    sanitize_integration_text_blocks,
)
from app.services.search_service import _screening_source_text, projected_search_sources
from app.tenant_scope import set_organization_context
from test_integration_auth_helpers import make_context, named_auth
from test_integration_read_api import _client
from test_integration_read_service import seed_read_candidates


@pytest.fixture
def privacy_context(tmp_path):
    context = make_context(tmp_path)
    with context.database.session_factory() as session:
        auth = named_auth(session, context)
        scopes = ["candidates:read", "jobs:read", "assessments:read", "evidence:read"]
        update_integration_policy(session, auth, settings=context.settings,
            payload=IntegrationPolicyPatch(enabled=True, allowed_scopes=scopes))
        context.token = create_integration_grant(session, auth, settings=context.settings,
            payload=IntegrationGrantCreate(name="Synthetic privacy regression", scopes=scopes)).token
        context.ids = seed_read_candidates(session, context.organization_id)
    yield context
    context.database.dispose()


@pytest.mark.parametrize("label", ["身份证号码：", "护照号：", "passport number: ", "home address: ", "姓名：",
    "微信：", "QQ：", "WeChat ID = ", "Telegram ", "微信号\n", "WhatsApp:\n"])
@pytest.mark.parametrize("surface", ["profile", "evidence"])
def test_inline_private_values_are_omitted(privacy_context, label, surface):
    context = privacy_context
    marker = "SYNTHETIC-PRIVATE-ONLY"
    text = "Built warehouse automation; " + label + marker
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        resume = session.scalar(select(Resume).where(Resume.candidate_id == context.ids["alpha"]))
        snapshot = session.scalar(select(ResumeFactSnapshot).where(ResumeFactSnapshot.resume_id == resume.id))
        facts = json.loads(snapshot.canonical_facts_json)
        facts["experiences"][0]["detail_items"][0]["detail_raw"] = text
        snapshot.canonical_facts_json = json.dumps(facts)
        block = session.scalar(select(ResumeSourceBlock).where(
            ResumeSourceBlock.resume_id == resume.id, ResumeSourceBlock.block_id == "alpha-main"))
        block.text = text
        session.commit()
    with _client(context) as client:
        headers = {"Authorization": f"Bearer {context.token}"}
        path = "/v1/integrations/candidates/" + context.ids["alpha"]
        response = (client.get(path, headers=headers) if surface == "profile" else
            client.post(path + "/evidence", headers=headers, json={"source_block_ids": ["alpha-main"]}))
        assert response.status_code == 200
        assert marker not in response.text
        if surface == "evidence":
            assert response.json()["excerpts"][0]["omitted"] is True
        else:
            assert any("details[0]" in field for field in response.json()["omitted_fields"])


@pytest.mark.parametrize("mode", ["all", "any"])
def test_search_uses_redacted_sources_without_identity_dependent_rejection(privacy_context, mode):
    context = privacy_context
    with _client(context) as client:
        headers = {"Authorization": f"Bearer {context.token}"}
        for name in ("Alice Secret", "Alice", "Never Present Person"):
            response = client.post("/v1/integrations/candidates/search", headers=headers,
                json={"keywords_all_of": [name], "condition_match_mode": mode})
            assert response.status_code == 200
            assert response.json()["total_count"] == 0
        job_query = client.post("/v1/integrations/candidates/search", headers=headers,
            json={"keywords_all_of": ["measurable"], "condition_match_mode": mode})
        assert job_query.status_code == 200
        assert job_query.json()["total_count"] == 3


def test_private_line_omission_preserves_separate_work_fact_and_normalizes_unicode():
    text, _ = sanitize_integration_text("项目交付完成\n护\u200b照号码：SYNTHETIC\n技能 Python", max_chars=500)
    assert text == "项目交付完成 技能 Python"
    value, _ = sanitize_integration_text("ＳＳＮ：０００－００－００００", max_chars=500)
    assert value is None


@pytest.mark.parametrize("private_line", [
    "性别 男",
    "出生日期 1990-01-02",
    "姓名 张三",
    "Gender Female",
    "Date of Birth 1990-01-02",
    "Email applicant@example.test",
])
def test_whitespace_delimited_private_labels_drop_full_line(private_line):
    text, _ = sanitize_integration_text(
        f"{private_line}\n项目交付完成\n技能 Python", max_chars=500,
    )
    assert text == "项目交付完成 技能 Python"


@pytest.mark.parametrize("address", [
    "南山区合成科技园18号",
    "• 合成花园88栋501室",
    "合成花园88栋 501 室",
    "合成花园88号楼501室",
    "合成花园88栋，501室",
    "12A Synthetic Road",
    "Currently live at 123 Synthetic Road, Apt 12A-3, Springfield, IL 62704",
    "Built work— Address 123 Synthetic Road",
])
def test_unlabelled_high_confidence_address_lines_are_omitted(address):
    assert sanitize_integration_text(address, max_chars=500)[0] is None


@pytest.mark.parametrize("text", [
    "项目规范；科技园18号SOP流程",
    "memory-address 64 bit indexes",
    "Built 2024 Google Drive integration project",
    "Certificate Authority, CA",
    "Unit testing",
])
def test_address_rules_preserve_ordinary_work_text(text):
    assert sanitize_integration_text(text, max_chars=500)[0] == text


def test_ordered_source_block_sanitization_carries_address_context():
    projected = sanitize_integration_text_blocks(
        [
            ("page-1", 1, "Address:\n123 Synthetic Road,"),
            ("page-2", 2, "Apt 12A-3\nSpringfield, IL 62704\nSkills Python"),
        ],
        max_chars=500,
    )
    assert projected["page-1"][0] is None
    assert projected["page-2"][0] == "Skills Python"


def test_ordered_source_block_sanitization_drops_cjk_address_continuation():
    projected = sanitize_integration_text_blocks(
        [
            ("page-1", 1, "合成路123号"),
            ("page-2", 2, "1室\n技能 Python"),
        ],
        max_chars=500,
    )
    assert projected["page-1"][0] is None
    assert projected["page-2"][0] == "技能 Python"


def test_evidence_omits_address_context_in_unrequested_prior_block(privacy_context):
    context = privacy_context
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        resume = session.scalar(select(Resume).where(Resume.candidate_id == context.ids["alpha"]))
        main = session.scalar(select(ResumeSourceBlock).where(
            ResumeSourceBlock.resume_id == resume.id,
            ResumeSourceBlock.block_id == "alpha-main",
        ))
        continuation = session.scalar(select(ResumeSourceBlock).where(
            ResumeSourceBlock.resume_id == resume.id,
            ResumeSourceBlock.block_id == "alpha-identity",
        ))
        main.page_no = 1
        main.text = "Address:\n123 Synthetic Road,"
        continuation.page_no = 2
        continuation.text = "Apt 12A-3\nSpringfield, IL 62704\nSkills Python"
        session.commit()
    with _client(context) as client:
        response = client.post(
            f"/v1/integrations/candidates/{context.ids['alpha']}/evidence",
            headers={"Authorization": f"Bearer {context.token}"},
            json={"source_block_ids": ["alpha-identity"]},
        )
    assert response.status_code == 200
    assert "Synthetic Road" not in response.text
    assert "Springfield" not in response.text
    assert "Skills Python" in response.text


def test_search_cannot_locate_candidate_by_address_continuation(privacy_context):
    context = privacy_context
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        resume = session.scalar(select(Resume).where(Resume.candidate_id == context.ids["alpha"]))
        main = session.scalar(select(ResumeSourceBlock).where(
            ResumeSourceBlock.resume_id == resume.id,
            ResumeSourceBlock.block_id == "alpha-main",
        ))
        continuation = session.scalar(select(ResumeSourceBlock).where(
            ResumeSourceBlock.resume_id == resume.id,
            ResumeSourceBlock.block_id == "alpha-identity",
        ))
        main.page_no = 1
        main.text = "Address:\n123 Synthetic Road,"
        continuation.page_no = 2
        continuation.text = "Apt 12A-3\nSpringfield, IL 62704\nSkills Python"
        session.commit()
    with _client(context) as client:
        response = client.post(
            "/v1/integrations/candidates/search",
            headers={"Authorization": f"Bearer {context.token}"},
            json={"keywords_all_of": ["Springfield"]},
        )
    assert response.status_code == 200
    assert response.json()["total_count"] == 0


def test_candidate_profile_does_not_expose_internal_resume_fingerprint(privacy_context):
    with _client(privacy_context) as client:
        response = client.get(
            f"/v1/integrations/candidates/{privacy_context.ids['alpha']}",
            headers={"Authorization": f"Bearer {privacy_context.token}"},
        )
    assert response.status_code == 200
    assert "facts_sha256" not in response.json()
    assert "canonical_facts_sha256" not in response.json()


@pytest.mark.parametrize("contact", [
    "微信：synthetic_user_42",
    "ＱＱ：１２３４５６７８",
    "We\u200bChat ID = synthetic_user_42",
    "Telegram synthetic_user_42",
    "微信号\n\nsynthetic_user_42",
    "WhatsApp:\nsynthetic_user_42",
    "联络 微信：\nsynthetic_user_42",
    "https://wa.me/12345678",
    "https://t.me/synthetic_user_42",
    "weixin://synthetic_user_42",
    "微信\nQQ\n12345678",
    "LINE ID synthetic_user_42",
    "Signal: synthetic_user_42",
])
def test_social_contacts_are_omitted_without_losing_separate_work_facts(contact):
    text, _ = sanitize_integration_text(
        "项目交付完成\n" + contact + "\n技能 Python", max_chars=500,
    )
    assert text == "项目交付完成 技能 Python"


@pytest.mark.parametrize("text", [
    "Built a command line tool", "signal processing engineer", "Python pipeline development",
])
def test_ordinary_line_and_signal_work_facts_are_not_contact_labels(text):
    assert sanitize_integration_text(text, max_chars=500)[0] == text


@pytest.mark.parametrize("mode", ["all", "any"])
def test_social_contact_cannot_be_used_to_locate_a_candidate(privacy_context, mode):
    context = privacy_context
    marker = "synthetic_social_handle_42"
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        resume = session.scalar(select(Resume).where(Resume.candidate_id == context.ids["alpha"]))
        resume.raw_text = (resume.raw_text or "") + "\n微信：" + marker
        block = session.scalar(select(ResumeSourceBlock).where(
            ResumeSourceBlock.resume_id == resume.id, ResumeSourceBlock.block_id == "alpha-main"))
        block.text += "\n微信：" + marker
        session.commit()
    with _client(context) as client:
        response = client.post("/v1/integrations/candidates/search",
            headers={"Authorization": f"Bearer {context.token}"},
            json={"keywords_all_of": [marker], "condition_match_mode": mode})
        assert response.status_code == 200
        assert response.json()["total_count"] == 0


@pytest.mark.parametrize("name", ["Ａｌｉｃｅ Ｓｅｃｒｅｔ", "Alice\u200b Secret", "Alice  Secret", "Alice\x00Secret"])
def test_candidate_name_uses_the_same_unicode_form_as_projected_text(name):
    text, _ = sanitize_integration_text(
        "Ａｌｉｃｅ Ｓｅｃｒｅｔ delivers Python projects",
        candidate_name=name,
        max_chars=500,
    )
    assert text == "delivers Python projects"


@pytest.mark.parametrize("name,text", [
    ("A\u200b\u0301lice Secret", "Álice Secret delivers Python projects"),
    ("Álice Secret", "A\u200b\u0301lice Secret delivers Python projects"),
])
def test_name_and_text_are_normalized_after_invisible_characters_are_removed(name, text):
    assert sanitize_integration_text(text, candidate_name=name, max_chars=500)[0] == "delivers Python projects"


def test_external_search_projection_does_not_change_or_leak_into_web_search(privacy_context):
    context = privacy_context
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        resume = session.scalar(select(Resume).where(Resume.candidate_id == context.ids["alpha"]))
        baseline = _screening_source_text(resume)
        assert "Alice Secret" in baseline
        with pytest.raises(RuntimeError):
            with projected_search_sources(lambda _resume, _text: "SAFE"):
                assert "Alice Secret" not in _screening_source_text(resume)
                raise RuntimeError("synthetic interrupted search")
        assert _screening_source_text(resume) == baseline


def test_connection_uses_configured_origin_and_never_audits_client_request_id(privacy_context):
    context = privacy_context
    with _client(context) as client:
        response = client.get("/v1/integrations/connection", headers={
            "Authorization": f"Bearer {context.token}", "X-Request-ID": "private-untrusted-value"})
        assert response.status_code == 200
        assert response.json()["api_base_url"] == "http://testserver/v1/integrations"
        assert response.json()["mcp_url"] == "http://testserver/v1/mcp"
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        assert all(row.request_id != "private-untrusted-value" for row in
            session.scalars(select(IntegrationAuditEvent)).all())
