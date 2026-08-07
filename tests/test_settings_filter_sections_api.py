"""Per-user filter-panel section preference API coverage.

A signed-in member (admin *or* recruiter) can read and write which
"初筛条件板块" (filter panel sections) stay visible under
``/v1/settings/filter-sections``. Like the display-field preference it is
per ``(user_id, organization_id)``; a member with no saved selection sees every
section (the product default), and an explicitly saved empty list hides the
whole panel.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppSettings
from app.main import create_app
from app.models import OrganizationMembership, UserAccount, utcnow
from app.services.identity_service import hash_password
from app.tenant_scope import set_organization_context
from test_candidate_data_lifecycle import _register_and_login


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """A member settings client with real per-workspace authentication.

    The shared conftest ``client`` fixture enables ``allow_unauthenticated``,
    which resolves every request to the local development identity in the
    legacy workspace. Per-user settings tests need real membership-bound
    sessions, so this module overrides the fixture with the same ephemeral
    database shape used by the candidate-data lifecycle tests.
    """

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=False,
        session_secret="settings-filter-sections-test-session-secret",
        transactional_email_provider="test",
        public_app_url="http://testserver",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _member_client(client: TestClient) -> TestClient:
    # register + login a workspace owner, set client.auth cookies, return client
    _register_and_login(
        client,
        organization_name="Filter Sections Org",
        email="filter-sections-member@example.test",
    )
    return client


def _create_same_workspace_member_client(
    owner_client: TestClient,
    *,
    organization_id: str,
    email: str,
) -> TestClient:
    """Create a second verified recruiter in the owner's existing workspace.

    Two members in the same workspace must keep independent panel-section
    preferences, mirroring the display-field isolation guarantee.
    """

    password = "filter-sections-member-password"
    database = owner_client.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, organization_id)
        member = UserAccount(
            email=email,
            email_key=email.casefold(),
            full_name="Filter sections workspace recruiter",
            password_hash=hash_password(password),
            email_verified_at=utcnow(),
        )
        session.add(member)
        session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization_id,
                user_id=member.id,
                role="recruiter",
            )
        )
        session.commit()

    member_client = TestClient(owner_client.app)
    logged_in = member_client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text
    return member_client


def test_filter_sections_defaults_to_all_sections(client):
    c = _member_client(client)
    response = c.get("/v1/settings/filter-sections")
    assert response.status_code == 200, response.text
    assert response.json()["filter_section_keys"] == [
        "condition_mode",
        "institution",
        "basic_profile",
        "academic",
        "graduation",
        "experience",
        "source_channel",
        "keywords",
    ]


def test_filter_sections_save_and_read(client):
    c = _member_client(client)
    response = c.put(
        "/v1/settings/filter-sections",
        json={
            "filter_section_keys": [
                "institution",
                "experience",
                "keywords",
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["filter_section_keys"] == [
        "institution",
        "experience",
        "keywords",
    ]
    assert (
        c.get("/v1/settings/filter-sections").json()["filter_section_keys"]
        == ["institution", "experience", "keywords"]
    )


def test_filter_sections_save_empty_clears_selection(client):
    c = _member_client(client)
    saved = c.put(
        "/v1/settings/filter-sections",
        json={"filter_section_keys": ["academic"]},
    )
    assert saved.status_code == 200, saved.text
    cleared = c.put(
        "/v1/settings/filter-sections",
        json={"filter_section_keys": []},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["filter_section_keys"] == []
    assert (
        c.get("/v1/settings/filter-sections").json()["filter_section_keys"] == []
    )


def test_filter_sections_reject_unknown_key(client):
    c = _member_client(client)
    response = c.put(
        "/v1/settings/filter-sections",
        json={"filter_section_keys": ["not_a_real_section"]},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "unknown_filter_section_key"


def test_filter_sections_reject_any_unknown_key_among_valid(client):
    c = _member_client(client)
    response = c.put(
        "/v1/settings/filter-sections",
        json={"filter_section_keys": ["institution", "experience", "not_a_real_section"]},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "unknown_filter_section_key"


def test_filter_sections_dedupe_preserving_order(client):
    c = _member_client(client)
    response = c.put(
        "/v1/settings/filter-sections",
        json={"filter_section_keys": ["keywords", "academic", "keywords", "institution"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["filter_section_keys"] == [
        "keywords",
        "academic",
        "institution",
    ]


def test_filter_sections_per_user_isolation(client):
    owner = _register_and_login(
        client,
        organization_name="Filter Sections Isolation Org",
        email="filter-sections-owner@example.test",
    )
    organization_id = owner["organization"]["organization_id"]
    colleague_client = _create_same_workspace_member_client(
        client,
        organization_id=organization_id,
        email="filter-sections-colleague@example.test",
    )
    try:
        # The owner (workspace admin) hides the academic section.
        saved = client.put(
            "/v1/settings/filter-sections",
            json={"filter_section_keys": ["institution", "experience"]},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["filter_section_keys"] == [
            "institution",
            "experience",
        ]

        # The colleague in the same workspace keeps the full-section default.
        colleague_defaults = colleague_client.get("/v1/settings/filter-sections")
        assert colleague_defaults.status_code == 200, colleague_defaults.text
        assert colleague_defaults.json()["filter_section_keys"] == [
            "condition_mode",
            "institution",
            "basic_profile",
            "academic",
            "graduation",
            "experience",
            "source_channel",
            "keywords",
        ]

        # The colleague can save their own independent selection.
        colleague_saved = colleague_client.put(
            "/v1/settings/filter-sections",
            json={"filter_section_keys": ["keywords"]},
        )
        assert colleague_saved.status_code == 200, colleague_saved.text
        assert colleague_saved.json()["filter_section_keys"] == ["keywords"]

        # The owner's selection is unchanged by the colleague's write.
        owner_again = client.get("/v1/settings/filter-sections")
        assert owner_again.status_code == 200, owner_again.text
        assert owner_again.json()["filter_section_keys"] == [
            "institution",
            "experience",
        ]
    finally:
        colleague_client.close()
