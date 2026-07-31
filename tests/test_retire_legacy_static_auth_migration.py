from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, select


_LEGACY_ORGANIZATION_ID = "00000000-0000-4000-8000-000000000001"
_LEGACY_USER_ID = "00000000-0000-4000-8000-000000000002"
_LEGACY_MEMBERSHIP_ID = "00000000-0000-4000-8000-000000000003"
_ADOPTING_ADMIN_IDS = ("formal-platform-admin-a", "formal-platform-admin-b")


def _sqlite_utc(value: datetime) -> datetime:
    """Normalize SQLite's timezone-less DateTime round-trip for comparisons."""

    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])
    return config


def _insert_user(
    connection,
    users: Table,
    *,
    user_id: str,
    now: datetime,
    active: bool = True,
    platform_admin: bool = True,
    verified: bool = True,
) -> None:
    connection.execute(
        users.insert(),
        {
            "id": user_id,
            "email": f"{user_id}@example.test",
            "email_key": f"{user_id}@example.test",
            "full_name": user_id,
            "password_hash": "migration-fixture-not-a-login-password",
            "is_active": active,
            "is_platform_admin": platform_admin,
            "email_verified_at": now if verified else None,
            "last_login_at": None,
            "created_at": now,
            "updated_at": now,
        },
    )


def _seed_historical_resume(connection, *, now: datetime) -> int:
    """Create only opaque fixtures needed to prove no source row moves."""

    metadata = MetaData()
    candidates = Table("candidates", metadata, autoload_with=connection)
    resumes = Table("resumes", metadata, autoload_with=connection)
    connection.execute(
        candidates.insert(),
        {
            "id": "retired-auth-candidate",
            "organization_id": _LEGACY_ORGANIZATION_ID,
            "display_name": "Synthetic retained candidate",
            "created_at": now,
            "retention_hold": False,
            "lifecycle_version": 1,
        },
    )
    connection.execute(
        resumes.insert(),
        {
            "id": "retired-auth-resume",
            "organization_id": _LEGACY_ORGANIZATION_ID,
            "candidate_id": "retired-auth-candidate",
            "original_filename": "synthetic-retained.pdf",
            "storage_key": "legacy/original/synthetic-retained.pdf",
            "sha256": "a" * 64,
            "source_page_count": 1,
            "parsed_page_count": 1,
            "extraction_status": "ready",
            "quality_flags": [],
            "parser_version": "retirement-test",
            "is_active": True,
            "employment_months": 0,
            "employment_or_internship_months": 0,
            "facts_version": 1,
            "raw_text": "Synthetic retained migration fixture.",
            "created_at": now,
            "updated_at": now,
            "retention_hold": False,
            "lifecycle_version": 1,
            "contact_details": [],
        },
    )
    users = Table("user_accounts", metadata, autoload_with=connection)
    return int(
        connection.execute(
            select(users.c.auth_session_version).where(users.c.id == _LEGACY_USER_ID)
        ).scalar_one()
    )


def _seed_legacy_invitations(connection, *, now: datetime) -> datetime:
    """Seed one pending and one accepted legacy invite for revocation coverage."""

    metadata = MetaData()
    invitations = Table("organization_invitations", metadata, autoload_with=connection)
    pending_expiry = now + timedelta(days=7)
    connection.execute(
        invitations.insert(),
        [
            {
                "id": "retire-legacy-pending-invitation",
                "organization_id": _LEGACY_ORGANIZATION_ID,
                "email_key": "pending-invite@example.test",
                "token_digest": "b" * 64,
                "role": "recruiter",
                "expires_at": pending_expiry,
                "accepted_at": None,
                "accepted_by_user_id": None,
                "created_by_user_id": None,
                "created_at": now,
            },
            {
                "id": "retire-legacy-accepted-invitation",
                "organization_id": _LEGACY_ORGANIZATION_ID,
                "email_key": "accepted-invite@example.test",
                "token_digest": "c" * 64,
                "role": "recruiter",
                "expires_at": pending_expiry,
                "accepted_at": now,
                "accepted_by_user_id": None,
                "created_by_user_id": None,
                "created_at": now,
            },
        ],
    )
    return pending_expiry


def test_retirement_allows_an_untouched_bootstrap_database_to_reach_head(tmp_path) -> None:
    """A new installation has no history that needs an adopted administrator."""

    database_path = tmp_path / "retire-legacy-pristine-bootstrap.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = _config(database_url)

    # A full upgrade creates only the deterministic legacy bootstrap identity
    # plus its automatic retention policy.  The retirement migration must not
    # make fresh/staging initialization impossible just because no real person
    # has registered yet.
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        users = Table("user_accounts", metadata, autoload_with=engine)
        memberships = Table("organization_memberships", metadata, autoload_with=engine)
        candidates = Table("candidates", metadata, autoload_with=engine)
        with engine.connect() as connection:
            legacy_user = connection.execute(
                select(users.c.is_active, users.c.is_platform_admin).where(
                    users.c.id == _LEGACY_USER_ID
                )
            ).one()
            legacy_membership_active = connection.execute(
                select(memberships.c.is_active).where(
                    memberships.c.id == _LEGACY_MEMBERSHIP_ID
                )
            ).scalar_one()
            candidate_count = connection.execute(select(candidates.c.id)).all()
    finally:
        engine.dispose()

    assert legacy_user == (False, False)
    assert legacy_membership_active is False
    assert candidate_count == []


def test_retirement_requires_adoption_when_bootstrap_retention_policy_changed(tmp_path) -> None:
    """A settings-only legacy workspace must not lose its only access path."""

    database_path = tmp_path / "retire-legacy-changed-retention-policy.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = _config(database_url)
    command.upgrade(config, "20260730_0052")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        policies = Table(
            "candidate_data_retention_policies", metadata, autoload_with=engine
        )
        users = Table("user_accounts", metadata, autoload_with=engine)
        memberships = Table("organization_memberships", metadata, autoload_with=engine)
        with engine.begin() as connection:
            original_auth_version = connection.execute(
                select(users.c.auth_session_version).where(users.c.id == _LEGACY_USER_ID)
            ).scalar_one()
            connection.execute(
                policies.update()
                .where(policies.c.organization_id == _LEGACY_ORGANIZATION_ID)
                .values(
                    mode="automatic",
                    retention_days=90,
                    version=2,
                    updated_by_user_id=_LEGACY_USER_ID,
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(
        RuntimeError,
        match="legacy_workspace_adoption_requires_verified_platform_admin",
    ):
        command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        policies = Table(
            "candidate_data_retention_policies", metadata, autoload_with=engine
        )
        users = Table("user_accounts", metadata, autoload_with=engine)
        memberships = Table("organization_memberships", metadata, autoload_with=engine)
        with engine.connect() as connection:
            legacy_user = connection.execute(
                select(
                    users.c.is_active,
                    users.c.is_platform_admin,
                    users.c.auth_session_version,
                ).where(users.c.id == _LEGACY_USER_ID)
            ).one()
            legacy_membership_active = connection.execute(
                select(memberships.c.is_active).where(
                    memberships.c.id == _LEGACY_MEMBERSHIP_ID
                )
            ).scalar_one()
            policy = connection.execute(
                select(
                    policies.c.mode,
                    policies.c.retention_days,
                    policies.c.version,
                    policies.c.updated_by_user_id,
                ).where(policies.c.organization_id == _LEGACY_ORGANIZATION_ID)
            ).one()
    finally:
        engine.dispose()

    assert legacy_user == (True, True, original_auth_version)
    assert legacy_membership_active is True
    assert policy == ("automatic", 90, 2, _LEGACY_USER_ID)


def test_retirement_adopts_every_eligible_platform_admin_and_preserves_history(tmp_path) -> None:
    database_path = tmp_path / "retire-legacy-static-auth.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = _config(database_url)
    command.upgrade(config, "20260730_0052")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        users = Table("user_accounts", metadata, autoload_with=engine)
        memberships = Table("organization_memberships", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)
        with engine.begin() as connection:
            original_auth_version = _seed_historical_resume(connection, now=now)
            pending_invitation_expiry = _seed_legacy_invitations(connection, now=now)
            for user_id in _ADOPTING_ADMIN_IDS:
                _insert_user(connection, users, user_id=user_id, now=now)
            # The migration must also repair a dormant legacy membership for a
            # qualified platform admin, rather than leaving a second path
            # inactive or with a recruiter-only role.
            connection.execute(
                memberships.insert(),
                {
                    "id": "formal-admin-b-old-membership",
                    "organization_id": _LEGACY_ORGANIZATION_ID,
                    "user_id": _ADOPTING_ADMIN_IDS[1],
                    "role": "recruiter",
                    "is_active": False,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            _insert_user(
                connection,
                users,
                user_id="unverified-platform-admin",
                now=now,
                verified=False,
            )
            _insert_user(
                connection,
                users,
                user_id="inactive-platform-admin",
                now=now,
                active=False,
            )
            _insert_user(
                connection,
                users,
                user_id="verified-non-platform-user",
                now=now,
                platform_admin=False,
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        organizations = Table("organizations", metadata, autoload_with=engine)
        users = Table("user_accounts", metadata, autoload_with=engine)
        memberships = Table("organization_memberships", metadata, autoload_with=engine)
        candidates = Table("candidates", metadata, autoload_with=engine)
        resumes = Table("resumes", metadata, autoload_with=engine)
        audits = Table("platform_audit_events", metadata, autoload_with=engine)
        invitations = Table("organization_invitations", metadata, autoload_with=engine)
        with engine.connect() as connection:
            organization = connection.execute(
                select(organizations).where(organizations.c.id == _LEGACY_ORGANIZATION_ID)
            ).mappings().one()
            legacy_user = connection.execute(
                select(users).where(users.c.id == _LEGACY_USER_ID)
            ).mappings().one()
            legacy_membership = connection.execute(
                select(memberships).where(memberships.c.id == _LEGACY_MEMBERSHIP_ID)
            ).mappings().one()
            adopted = connection.execute(
                select(memberships)
                .where(
                    memberships.c.organization_id == _LEGACY_ORGANIZATION_ID,
                    memberships.c.user_id.in_(_ADOPTING_ADMIN_IDS),
                )
                .order_by(memberships.c.user_id)
            ).mappings().all()
            candidate = connection.execute(
                select(candidates).where(candidates.c.id == "retired-auth-candidate")
            ).mappings().one()
            resume = connection.execute(
                select(resumes).where(resumes.c.id == "retired-auth-resume")
            ).mappings().one()
            audit_rows = connection.execute(
                select(audits)
                .where(
                    audits.c.action == "legacy_workspace_adopted",
                    audits.c.organization_id == _LEGACY_ORGANIZATION_ID,
                )
                .order_by(audits.c.actor_user_id)
            ).mappings().all()
            pending_invitation = connection.execute(
                select(invitations).where(
                    invitations.c.id == "retire-legacy-pending-invitation"
                )
            ).mappings().one()
            accepted_invitation = connection.execute(
                select(invitations).where(
                    invitations.c.id == "retire-legacy-accepted-invitation"
                )
            ).mappings().one()
            invitation_revocation_audit = connection.execute(
                select(audits).where(
                    audits.c.action == "legacy_workspace_invitations_revoked",
                    audits.c.organization_id == _LEGACY_ORGANIZATION_ID,
                )
            ).mappings().one()
    finally:
        engine.dispose()

    assert organization["plan_status"] == "active"
    assert legacy_user["is_active"] is False
    assert legacy_user["is_platform_admin"] is False
    assert legacy_user["auth_session_version"] == original_auth_version + 1
    assert legacy_membership["is_active"] is False
    assert [(row["user_id"], row["role"], row["is_active"]) for row in adopted] == [
        (_ADOPTING_ADMIN_IDS[0], "admin", True),
        (_ADOPTING_ADMIN_IDS[1], "admin", True),
    ]
    assert [row["actor_user_id"] for row in audit_rows] == list(_ADOPTING_ADMIN_IDS)
    assert all(row["reason"] == "legacy_static_auth_retirement" for row in audit_rows)
    assert all(row["after_json"]["is_active"] is True for row in audit_rows)
    assert pending_invitation["accepted_at"] is None
    assert _sqlite_utc(pending_invitation["expires_at"]) < _sqlite_utc(
        pending_invitation_expiry
    )
    assert accepted_invitation["accepted_at"] is not None
    assert _sqlite_utc(accepted_invitation["expires_at"]) == _sqlite_utc(
        pending_invitation_expiry
    )
    assert invitation_revocation_audit["reason"] == "legacy_static_auth_retirement"
    assert invitation_revocation_audit["before_json"] == {"pending_invitation_count": 1}
    assert invitation_revocation_audit["after_json"] == {"pending_invitation_count": 0}
    assert candidate["organization_id"] == _LEGACY_ORGANIZATION_ID
    assert candidate["display_name"] == "Synthetic retained candidate"
    assert resume["organization_id"] == _LEGACY_ORGANIZATION_ID
    assert resume["storage_key"] == "legacy/original/synthetic-retained.pdf"
    assert resume["original_filename"] == "synthetic-retained.pdf"

    # Rolling code back must never revive a shared credential or remove a
    # formal administrator's explicit access to the preserved workspace.
    command.downgrade(config, "20260730_0052")
    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        users = Table("user_accounts", metadata, autoload_with=engine)
        memberships = Table("organization_memberships", metadata, autoload_with=engine)
        with engine.connect() as connection:
            legacy_state = connection.execute(
                select(users.c.is_active, users.c.is_platform_admin).where(
                    users.c.id == _LEGACY_USER_ID
                )
            ).one()
            adopted_count = connection.execute(
                select(memberships.c.id).where(
                    memberships.c.organization_id == _LEGACY_ORGANIZATION_ID,
                    memberships.c.user_id.in_(_ADOPTING_ADMIN_IDS),
                    memberships.c.is_active.is_(True),
                )
            ).all()
    finally:
        engine.dispose()

    assert legacy_state == (False, False)
    assert len(adopted_count) == len(_ADOPTING_ADMIN_IDS)


def test_retirement_fails_before_any_state_change_without_eligible_administrator(tmp_path) -> None:
    database_path = tmp_path / "retire-legacy-without-adopter.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = _config(database_url)
    command.upgrade(config, "20260730_0052")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        users = Table("user_accounts", metadata, autoload_with=engine)
        memberships = Table("organization_memberships", metadata, autoload_with=engine)
        organizations = Table("organizations", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)
        with engine.begin() as connection:
            original_auth_version = _seed_historical_resume(connection, now=now)
            pending_invitation_expiry = _seed_legacy_invitations(connection, now=now)
            _insert_user(
                connection,
                users,
                user_id="unverified-only-platform-admin",
                now=now,
                verified=False,
            )
            original_organization_status = connection.execute(
                select(organizations.c.plan_status).where(
                    organizations.c.id == _LEGACY_ORGANIZATION_ID
                )
            ).scalar_one()
            original_membership_active = connection.execute(
                select(memberships.c.is_active).where(
                    memberships.c.id == _LEGACY_MEMBERSHIP_ID
                )
            ).scalar_one()
    finally:
        engine.dispose()

    with pytest.raises(
        RuntimeError,
        match="legacy_workspace_adoption_requires_verified_platform_admin",
    ):
        command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        users = Table("user_accounts", metadata, autoload_with=engine)
        memberships = Table("organization_memberships", metadata, autoload_with=engine)
        organizations = Table("organizations", metadata, autoload_with=engine)
        candidates = Table("candidates", metadata, autoload_with=engine)
        resumes = Table("resumes", metadata, autoload_with=engine)
        invitations = Table("organization_invitations", metadata, autoload_with=engine)
        with engine.connect() as connection:
            legacy_user = connection.execute(
                select(users).where(users.c.id == _LEGACY_USER_ID)
            ).mappings().one()
            membership_active = connection.execute(
                select(memberships.c.is_active).where(
                    memberships.c.id == _LEGACY_MEMBERSHIP_ID
                )
            ).scalar_one()
            organization_status = connection.execute(
                select(organizations.c.plan_status).where(
                    organizations.c.id == _LEGACY_ORGANIZATION_ID
                )
            ).scalar_one()
            candidate = connection.execute(
                select(candidates.c.organization_id).where(
                    candidates.c.id == "retired-auth-candidate"
                )
            ).scalar_one()
            resume = connection.execute(
                select(resumes.c.storage_key).where(resumes.c.id == "retired-auth-resume")
            ).scalar_one()
            pending_invitation = connection.execute(
                select(invitations.c.expires_at).where(
                    invitations.c.id == "retire-legacy-pending-invitation"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert legacy_user["is_active"] is True
    assert legacy_user["is_platform_admin"] is True
    assert legacy_user["auth_session_version"] == original_auth_version
    assert membership_active is original_membership_active is True
    assert organization_status == original_organization_status == "active"
    assert candidate == _LEGACY_ORGANIZATION_ID
    assert resume == "legacy/original/synthetic-retained.pdf"
    assert _sqlite_utc(pending_invitation) == _sqlite_utc(pending_invitation_expiry)
