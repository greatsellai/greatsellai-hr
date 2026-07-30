from __future__ import annotations

import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Organization, WorkspaceBackgroundLane
from app.services.workspace_background_lane_service import (
    acquire_workspace_background_lane,
)


_POSTGRES_TEST_URL = os.getenv("MAILBOX_DEDUP_POSTGRES_TEST_URL")


def _postgres_schema_factory():
    assert _POSTGRES_TEST_URL is not None
    schema_name = f"workspace_lane_{uuid4().hex}"
    admin_engine = create_engine(_POSTGRES_TEST_URL, pool_pre_ping=True)
    test_engine = None
    try:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema_name}"')
        test_engine = create_engine(
            _POSTGRES_TEST_URL,
            connect_args={"options": f"-csearch_path={schema_name}"},
            pool_pre_ping=True,
        )
        Base.metadata.create_all(test_engine)
        yield sessionmaker(
            bind=test_engine,
            autoflush=False,
            expire_on_commit=False,
        )
    finally:
        if test_engine is not None:
            test_engine.dispose()
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        admin_engine.dispose()


@pytest.mark.skipif(
    not _POSTGRES_TEST_URL,
    reason="set MAILBOX_DEDUP_POSTGRES_TEST_URL to a disposable PostgreSQL database",
)
def test_postgresql_workspace_lane_allows_only_one_concurrent_claim_per_workspace() -> None:
    """The unique row + conditional update must fence concurrent child workers."""

    schema_factory = _postgres_schema_factory()
    session_factory = next(schema_factory)
    threads: list[threading.Thread] = []
    errors: list[BaseException] = []
    claims: list[object | None] = []
    barrier = threading.Barrier(2)
    try:
        with session_factory() as session:
            organization = Organization(name="PostgreSQL single lane")
            session.add(organization)
            session.commit()
            organization_id = organization.id

        def claim(worker_id: str) -> None:
            try:
                with session_factory() as session:
                    barrier.wait(timeout=5)
                    value = acquire_workspace_background_lane(
                        session,
                        organization_id=organization_id,
                        worker_id=worker_id,
                        job_kind="document_extraction",
                        job_id=f"job-{worker_id}",
                        lease_seconds=180,
                    )
                    session.commit()
                    claims.append(value)
            except BaseException as exc:  # surfaced after both threads exit
                errors.append(exc)

        for worker_id in ("worker-a", "worker-b"):
            thread = threading.Thread(target=claim, args=(worker_id,), daemon=True)
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        winners = [claim for claim in claims if claim is not None]
        assert len(winners) == 1
        with session_factory() as session:
            lanes = session.scalars(select(WorkspaceBackgroundLane)).all()
            assert len(lanes) == 1
            assert lanes[0].lease_token == winners[0].lease_token
    finally:
        try:
            next(schema_factory)
        except StopIteration:
            pass


@pytest.mark.skipif(
    not _POSTGRES_TEST_URL,
    reason="set MAILBOX_DEDUP_POSTGRES_TEST_URL to a disposable PostgreSQL database",
)
def test_postgresql_workspace_lanes_allow_distinct_workspaces_in_parallel() -> None:
    """Fairness must not turn independent workspaces into a global mutex."""

    schema_factory = _postgres_schema_factory()
    session_factory = next(schema_factory)
    threads: list[threading.Thread] = []
    errors: list[BaseException] = []
    claims: list[object | None] = []
    barrier = threading.Barrier(2)
    try:
        with session_factory() as session:
            first = Organization(name="PostgreSQL first lane")
            second = Organization(name="PostgreSQL second lane")
            session.add_all((first, second))
            session.commit()
            organization_ids = (first.id, second.id)

        def claim(worker_id: str, organization_id: str) -> None:
            try:
                with session_factory() as session:
                    barrier.wait(timeout=5)
                    value = acquire_workspace_background_lane(
                        session,
                        organization_id=organization_id,
                        worker_id=worker_id,
                        job_kind="document_extraction",
                        job_id=f"job-{worker_id}",
                        lease_seconds=180,
                    )
                    session.commit()
                    claims.append(value)
            except BaseException as exc:  # surfaced after both threads exit
                errors.append(exc)

        for worker_id, organization_id in zip(
            ("worker-a", "worker-b"),
            organization_ids,
            strict=True,
        ):
            thread = threading.Thread(
                target=claim,
                args=(worker_id, organization_id),
                daemon=True,
            )
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert len(claims) == 2
        assert all(claim is not None for claim in claims)
        with session_factory() as session:
            lanes = session.scalars(select(WorkspaceBackgroundLane)).all()
            assert {lane.organization_id for lane in lanes} == set(organization_ids)
    finally:
        try:
            next(schema_factory)
        except StopIteration:
            pass
