"""Bounded SQL-cost evidence for ordinary integration candidate reads.

These tests use only synthetic SQLite data.  They intentionally measure two
enforcement points separately:

* ``search_candidates`` is the shared read projection used by REST and MCP.
* ``execute_integration_read`` adds authority revalidation, request/concurrency
  accounting, source-version fences, daily candidate accounting and audit.

The timer/listener starts after ``authenticate_integration_token``.  Thus the
guarded measurement is the post-auth service boundary; it excludes initial
token authentication, HTTP/MCP transport, browser/OAuth work and serialization.
The fixture gives each resume only one small source block and one skill.  Its
elapsed time is diagnostic evidence, not a latency or Jiaxin capacity SLA.
Counters record SQLAlchemy ``before_cursor_execute`` calls: an ``executemany``
call counts once although it can process multiple rows.  Constant cursor-call
counts do not imply constant row work, memory use or server round trips.

The regression invariant is that SELECT count must not grow with the number of
eligible or returned candidates; candidate relationships are expected to use
bounded eager-loading/fence queries rather than per-candidate SELECTs.  The
largest fixture is deliberately 120 rows, so this does not cover selectinload's
larger-dataset batching threshold (normally 500 parent identifiers).
"""

from __future__ import annotations

import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from statistics import median
from time import perf_counter
from typing import Callable, Iterator
from uuid import uuid4

from sqlalchemy import delete, event

from app.integration_read_schemas import (
    IntegrationCandidateSearchRequest,
    IntegrationCandidateSearchResponse,
)
from app.models import (
    Candidate,
    IntegrationAuditEvent,
    IntegrationDailyCandidateAccess,
    IntegrationRateLimitBucket,
    IntegrationRequestLease,
    Resume,
    ResumeFactSnapshot,
    ResumeSkill,
    ResumeSourceBlock,
)
from app.services.integration_auth_service import authenticate_integration_token
from app.services.integration_read_service import (
    execute_integration_read,
    search_candidates,
)
from test_integration_auth_helpers import make_context


DATASET_SIZES = (10, 50, 120)
PAGE_LIMITS = (20, 100)
CORE_SEARCH_SELECT_BUDGET = 15
GUARDED_READ_SELECT_BUDGET = 50
CORE_SEARCH_STATEMENT_BUDGET = 15
GUARDED_READ_STATEMENT_BUDGET = 63


@dataclass(frozen=True)
class QueryScenario:
    name: str
    request_fields: dict[str, object]
    expected_count: Callable[[int], int]


SCENARIOS = (
    QueryScenario("unfiltered", {}, lambda size: size),
    QueryScenario(
        "skill-python",
        {"skills_all_of": ["Python"]},
        lambda size: (size + 1) // 2,
    ),
    QueryScenario(
        "keyword-automation",
        {"keywords_all_of": ["automation"]},
        lambda size: (size + 1) // 2,
    ),
)


@dataclass(frozen=True)
class SqlMeasurement:
    dataset_size: int
    page_limit: int
    scenario: str
    enforcement_point: str
    statement_count: int
    operations: dict[str, int]
    elapsed_ms: float
    returned_count: int
    total_count: int

    def render(self) -> str:
        return json.dumps(
            {
                "dataset_size": self.dataset_size,
                "page_limit": self.page_limit,
                "scenario": self.scenario,
                "enforcement_point": self.enforcement_point,
                "statement_count": self.statement_count,
                "operations": self.operations,
                "elapsed_ms": round(self.elapsed_ms, 3),
                "returned_count": self.returned_count,
                "total_count": self.total_count,
            },
            sort_keys=True,
        )


@contextmanager
def _record_sql(engine) -> Iterator[tuple[list[str], float]]:
    operations: list[str] = []

    def before_cursor_execute(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        operation = statement.lstrip().partition(" ")[0].upper()
        operations.append(operation)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    started = perf_counter()
    try:
        yield operations, started
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def _seed_candidates(session, *, organization_id: str, count: int) -> None:
    rows: list[object] = []
    for index in range(count):
        candidate_id = str(uuid4())
        resume_id = str(uuid4())
        block_id = f"synthetic-{index:03d}"
        matches = index % 2 == 0
        skill_key = "python" if matches else "figma"
        skill_display = "Python" if matches else "Figma"
        source_text = (
            "Built warehouse automation with measurable results."
            if matches
            else "Created product design assets with measurable results."
        )
        rows.extend(
            [
                Candidate(
                    id=candidate_id,
                    organization_id=organization_id,
                    display_name=f"Synthetic Candidate {index:03d}",
                ),
                Resume(
                    id=resume_id,
                    organization_id=organization_id,
                    candidate_id=candidate_id,
                    original_filename=f"synthetic-{index:03d}.pdf",
                    storage_key=f"{organization_id}/query-budget-{index:03d}.pdf",
                    sha256=f"{index + 1:064x}",
                    source_page_count=1,
                    parsed_page_count=1,
                    extraction_status="ready",
                    quality_flags=[],
                    parser_version="integration-query-budget",
                    is_active=True,
                    is_985_211=False,
                    highest_degree="bachelor",
                    employment_months=index % 60,
                    employment_or_internship_months=index % 72,
                    facts_version=1,
                    raw_text=source_text,
                    contact_details=[],
                ),
                ResumeSourceBlock(
                    resume_id=resume_id,
                    block_id=block_id,
                    page_no=1,
                    block_type="paragraph",
                    text=source_text,
                ),
                ResumeSkill(
                    resume_id=resume_id,
                    skill_key=skill_key,
                    skill_display=skill_display,
                    skill_category="software" if matches else "design_content",
                    evidence_block_ids=[block_id],
                ),
                ResumeFactSnapshot(
                    organization_id=organization_id,
                    resume_id=resume_id,
                    facts_version=1,
                    canonical_facts_json=json.dumps(
                        {
                            "schema_version": "resume_facts.v1",
                            "source_block_ids": [block_id],
                        }
                    ),
                    facts_sha256=f"{count * 1000 + index + 1:064x}",
                    source_block_ids=[block_id],
                    created_by="integration-query-budget",
                ),
            ]
        )
    session.add_all(rows)
    session.commit()


def _clear_request_accounting(context) -> None:
    """Give every guarded measurement the same synthetic cold-accounting state.

    The database belongs only to this test context.  This reset neither models
    warm repeat access nor implies that production quota state can be bypassed.
    """

    with context.database.session_factory() as session:
        authenticate_integration_token(
            session,
            token=context.token,
            audience="rest",
            settings=context.settings,
            required_scopes=("candidates:read",),
        )
        for model in (
            IntegrationAuditEvent,
            IntegrationDailyCandidateAccess,
            IntegrationRequestLease,
            IntegrationRateLimitBucket,
        ):
            session.execute(delete(model))
        session.commit()


def _measure(
    context,
    *,
    dataset_size: int,
    page_limit: int,
    scenario: QueryScenario,
    guarded: bool,
) -> SqlMeasurement:
    request = IntegrationCandidateSearchRequest(
        **scenario.request_fields,
        limit=page_limit,
    )
    with context.database.session_factory() as session:
        principal = authenticate_integration_token(
            session,
            token=context.token,
            audience="rest",
            settings=context.settings,
            required_scopes=("candidates:read",),
        )

        def read() -> IntegrationCandidateSearchResponse:
            return search_candidates(
                session,
                principal=principal,
                settings=context.settings,
                request=request,
            )

        with _record_sql(context.database.engine) as (operations, started):
            if guarded:
                result = execute_integration_read(
                    session,
                    principal=principal,
                    settings=context.settings,
                    action="integration.candidates.search",
                    resource_type="candidate_search",
                    read=read,
                    resource_ids=lambda value: [
                        item.resume_id for item in value.items
                    ],
                    candidate_ids=lambda value: [
                        item.candidate_id for item in value.items
                    ],
                )
            else:
                result = read()
            elapsed_ms = (perf_counter() - started) * 1000

    expected_total = scenario.expected_count(dataset_size)
    assert result.total_count == expected_total
    assert len(result.items) == min(expected_total, page_limit)
    assert (result.next_cursor is not None) is (expected_total > page_limit)
    counts = Counter(operations)
    return SqlMeasurement(
        dataset_size=dataset_size,
        page_limit=page_limit,
        scenario=scenario.name,
        enforcement_point="guarded-read" if guarded else "core-search",
        statement_count=len(operations),
        operations=dict(sorted(counts.items())),
        elapsed_ms=elapsed_ms,
        returned_count=len(result.items),
        total_count=result.total_count,
    )


def test_candidate_read_select_budget_is_constant_for_bounded_datasets(
    tmp_path,
) -> None:
    measurements: list[SqlMeasurement] = []
    for dataset_size in DATASET_SIZES:
        context = make_context(tmp_path / f"dataset-{dataset_size}")
        try:
            with context.database.session_factory() as session:
                authenticate_integration_token(
                    session,
                    token=context.token,
                    audience="rest",
                    settings=context.settings,
                    required_scopes=("candidates:read",),
                )
                _seed_candidates(
                    session,
                    organization_id=context.organization_id,
                    count=dataset_size,
                )

            for scenario in SCENARIOS:
                for page_limit in PAGE_LIMITS:
                    measurements.append(
                        _measure(
                            context,
                            dataset_size=dataset_size,
                            page_limit=page_limit,
                            scenario=scenario,
                            guarded=False,
                        )
                    )
                    _clear_request_accounting(context)
                    measurements.append(
                        _measure(
                            context,
                            dataset_size=dataset_size,
                            page_limit=page_limit,
                            scenario=scenario,
                            guarded=True,
                        )
                    )
        finally:
            context.database.dispose()

    for measurement in measurements:
        print(f"QUERY_BUDGET {measurement.render()}")

    for enforcement_point in ("core-search", "guarded-read"):
        group = [
            measurement
            for measurement in measurements
            if measurement.enforcement_point == enforcement_point
        ]
        elapsed = [measurement.elapsed_ms for measurement in group]
        print(
            "QUERY_BUDGET_SUMMARY "
            + json.dumps(
                {
                    "enforcement_point": enforcement_point,
                    "measurement_count": len(group),
                    "select_counts": sorted(
                        {
                            measurement.operations.get("SELECT", 0)
                            for measurement in group
                        }
                    ),
                    "statement_counts": sorted(
                        {measurement.statement_count for measurement in group}
                    ),
                    "elapsed_ms_min": round(min(elapsed), 3),
                    "elapsed_ms_median": round(median(elapsed), 3),
                    "elapsed_ms_max": round(max(elapsed), 3),
                },
                sort_keys=True,
            )
        )

    # Compare like-for-like searches.  Page size and dataset growth must not
    # add per-candidate SELECTs or accounting writes on this fixed SQLite path.
    for enforcement_point in ("core-search", "guarded-read"):
        for scenario in SCENARIOS:
            select_counts = {
                measurement.operations.get("SELECT", 0)
                for measurement in measurements
                if measurement.enforcement_point == enforcement_point
                and measurement.scenario == scenario.name
            }
            assert len(select_counts) == 1, (
                enforcement_point,
                scenario.name,
                sorted(select_counts),
            )
            statement_counts = {
                measurement.statement_count
                for measurement in measurements
                if measurement.enforcement_point == enforcement_point
                and measurement.scenario == scenario.name
            }
            assert len(statement_counts) == 1, (
                enforcement_point,
                scenario.name,
                sorted(statement_counts),
            )

    core_select_counts = {
        measurement.operations.get("SELECT", 0)
        for measurement in measurements
        if measurement.enforcement_point == "core-search"
    }
    guarded_select_counts = {
        measurement.operations.get("SELECT", 0)
        for measurement in measurements
        if measurement.enforcement_point == "guarded-read"
    }
    core_statement_counts = {
        measurement.statement_count
        for measurement in measurements
        if measurement.enforcement_point == "core-search"
    }
    guarded_statement_counts = {
        measurement.statement_count
        for measurement in measurements
        if measurement.enforcement_point == "guarded-read"
    }
    assert max(core_select_counts) <= CORE_SEARCH_SELECT_BUDGET
    assert max(guarded_select_counts) <= GUARDED_READ_SELECT_BUDGET
    assert max(core_statement_counts) <= CORE_SEARCH_STATEMENT_BUDGET
    assert max(guarded_statement_counts) <= GUARDED_READ_STATEMENT_BUDGET

