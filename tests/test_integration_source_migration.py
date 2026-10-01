from __future__ import annotations

import importlib
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from app.database import Base
import app.models  # noqa: F401 - register the metadata under test


MIGRATION = "migrations.versions.20260927_0064_integration_api_mcp"
CONFIRMATION_MIGRATION = "migrations.versions.20261001_0066_integration_analysis_confirmation"
EXISTING_BINDINGS = {
    "organization_memberships": ("uq_integration_membership_binding", ("id", "organization_id", "user_id")),
    "candidates": ("uq_integration_candidate_org", ("id", "organization_id")),
    "resumes": ("uq_integration_resume_binding", ("id", "organization_id", "candidate_id")),
    "resume_fact_snapshots": ("uq_integration_snapshot_binding", ("id", "organization_id", "resume_id", "facts_version")),
    "job_versions": ("uq_integration_job_version_binding", ("id", "organization_id", "job_id")),
}


class _AdditiveSchemaRecorder:
    """Only create operations are available; existing schema cannot be changed.

    Referenced base tables intentionally start without the new binding indexes,
    so this also verifies that each composite FK's unique key exists before the
    dependent table is created, as required by PostgreSQL.
    """

    def __init__(self) -> None:
        self.metadata = sa.MetaData()
        self.created_tables: set[str] = set()
        for name in ("organizations", "user_accounts", *EXISTING_BINDINGS):
            model = Base.metadata.tables[name]
            sa.Table(
                name,
                self.metadata,
                *(
                    sa.Column(column.name, column.type, nullable=column.nullable, primary_key=column.primary_key)
                    for column in model.columns
                ),
            )

    def create_table(self, name: str, *items) -> sa.Table:
        table = sa.Table(name, self.metadata, *items)
        self.created_tables.add(name)
        for foreign_key in table.foreign_key_constraints:
            target = foreign_key.elements[0].column.table
            columns = tuple(element.column.name for element in foreign_key.elements)
            unique_keys = {tuple(target.primary_key.columns.keys())}
            unique_keys.update(
                tuple(constraint.columns.keys())
                for constraint in target.constraints
                if isinstance(constraint, sa.UniqueConstraint)
            )
            unique_keys.update(tuple(index.columns.keys()) for index in target.indexes if index.unique)
            assert columns in unique_keys, (name, foreign_key.name, columns)
        return table

    def create_index(self, name: str, table_name: str, columns: list[str], *, unique: bool = False) -> None:
        table = self.metadata.tables[table_name]
        sa.Index(name, *(table.c[column] for column in columns), unique=unique)


def _default(column: sa.Column) -> str | None:
    return None if column.server_default is None else str(column.server_default.arg)


def _indexes(table: sa.Table) -> set[tuple]:
    return {(index.name, tuple(index.columns.keys()), bool(index.unique)) for index in table.indexes}


def _constraints(table: sa.Table) -> set[tuple]:
    constraints = set()
    for constraint in table.constraints:
        if isinstance(constraint, sa.ForeignKeyConstraint):
            constraints.add((
                "foreign_key", constraint.name, tuple(constraint.columns.keys()),
                tuple(element.target_fullname for element in constraint.elements),
                constraint.ondelete, constraint.onupdate, constraint.deferrable, constraint.initially,
            ))
        elif isinstance(constraint, sa.UniqueConstraint):
            constraints.add(("unique", constraint.name, tuple(constraint.columns.keys())))
        elif isinstance(constraint, sa.CheckConstraint):
            constraints.add(("check", constraint.name, str(constraint.sqltext)))
        elif isinstance(constraint, sa.PrimaryKeyConstraint):
            constraints.add(("primary_key", constraint.name, tuple(constraint.columns.keys())))
        else:
            raise AssertionError(f"unreviewed constraint type: {type(constraint)}")
    return constraints


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()], ids=["postgresql", "sqlite"])
def test_integration_migration_matches_all_models_and_orders_binding_keys(monkeypatch, dialect) -> None:
    migration = importlib.import_module(MIGRATION)
    recorder = _AdditiveSchemaRecorder()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    expected_tables = {name for name in Base.metadata.tables if name.startswith("integration_")}
    assert len(expected_tables) == 16
    assert recorder.created_tables == expected_tables
    for name in expected_tables:
        expected = Base.metadata.tables[name]
        actual = recorder.metadata.tables[name]
        later_additions = {"last_authorized_at"} if name == "integration_oauth_clients" else set()
        if name == "integration_analysis_reports":
            later_additions |= {
                "source_credential_id", "source_audience",
                "source_auth_session_version", "confirmed_at",
            }
        expected_column_names = set(expected.c.keys()) - later_additions
        assert set(actual.c.keys()) == expected_column_names, name
        for column in expected.columns:
            if column.name in later_additions:
                continue
            migrated = actual.c[column.name]
            assert migrated.type.compile(dialect=dialect) == column.type.compile(dialect=dialect), (name, column.name)
            assert getattr(migrated.type, "timezone", None) == getattr(column.type, "timezone", None)
            assert migrated.nullable == column.nullable, (name, column.name)
            assert _default(migrated) == _default(column), (name, column.name)
        expected_constraints = _constraints(expected)
        expected_indexes = _indexes(expected)
        if name == "integration_analysis_reports":
            expected_constraints = {
                item for item in expected_constraints
                if item[1] != "ck_integration_report_pending_confirmation_binding"
            }
            expected_indexes = {
                item for item in expected_indexes
                if item[0] not in {
                    "ix_integration_analysis_reports_confirmed_at",
                    "ix_integration_report_pending",
                }
            }
        assert _constraints(actual) == expected_constraints, name
        assert _indexes(actual) == expected_indexes, name
        CreateTable(actual).compile(dialect=dialect)
        for index in actual.indexes:
            CreateIndex(index).compile(dialect=dialect)

    for table_name, (index_name, columns) in EXISTING_BINDINGS.items():
        table = recorder.metadata.tables[table_name]
        assert _indexes(table) == {(index_name, columns, True)}
        assert (index_name, columns, True) in _indexes(Base.metadata.tables[table_name])
        for index in table.indexes:
            CreateIndex(index).compile(dialect=dialect)


def test_integration_revision_extends_source_head_and_downgrade_is_non_destructive(monkeypatch) -> None:
    migration = importlib.import_module(MIGRATION)
    assert migration.revision == "20260927_0064"
    assert migration.down_revision == "20260806_0063"
    assert migration.branch_labels is None
    assert migration.depends_on is None
    # The guard must raise before any connection, query or destructive DDL.
    monkeypatch.setattr(migration, "op", SimpleNamespace())
    with pytest.raises(RuntimeError, match="integration_schema_downgrade_blocked"):
        migration.downgrade()


def test_integration_migration_upgrades_source_chain_without_altering_base_columns(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'integration-source-migration.sqlite').as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])
    command.upgrade(config, "20260806_0063")

    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        base_columns = {name: inspector.get_columns(name) for name in EXISTING_BINDINGS}
        command.upgrade(config, "20260927_0064")
        command.upgrade(config, "20260928_0065")
        command.upgrade(config, "20261001_0066")
        inspector = sa.inspect(engine)
        assert {name for name in inspector.get_table_names() if name.startswith("integration_")} == {
            name for name in Base.metadata.tables if name.startswith("integration_")
        }
        for name in EXISTING_BINDINGS:
            before = [(column["name"], str(column["type"]), column["nullable"], column["default"]) for column in base_columns[name]]
            after = [(column["name"], str(column["type"]), column["nullable"], column["default"]) for column in inspector.get_columns(name)]
            assert after == before, name
        for name, (index_name, columns) in EXISTING_BINDINGS.items():
            assert (index_name, columns, True) in {
                (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                for index in inspector.get_indexes(name)
            }
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "20261001_0066"
        assert "last_authorized_at" in {
            column["name"] for column in inspector.get_columns("integration_oauth_clients")
        }
        report_columns = {column["name"] for column in inspector.get_columns("integration_analysis_reports")}
        assert {"source_credential_id", "source_audience", "source_auth_session_version", "confirmed_at"} <= report_columns
        with pytest.raises(RuntimeError, match="integration_schema_downgrade_blocked"):
            command.downgrade(config, "20260928_0065")
        with pytest.raises(RuntimeError, match="integration_schema_downgrade_blocked"):
            command.downgrade(config, "20260806_0063")
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "20261001_0066"
        assert {name for name in sa.inspect(engine).get_table_names() if name.startswith("integration_")} == {
            name for name in Base.metadata.tables if name.startswith("integration_")
        }
    finally:
        engine.dispose()


def test_confirmation_migration_refuses_legacy_reports_before_any_schema_change(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'integration-legacy-confirmation.sqlite').as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])
    command.upgrade(config, "20260928_0065")

    engine = sa.create_engine(database_url)
    legacy_rows = [
        ("legacy-active", "2026-10-01 00:00:00", None),
        ("legacy-expired", "2025-01-01 00:00:00", None),
        ("legacy-invalidated", "2026-10-01 00:00:00", "2026-09-30 00:00:00"),
    ]
    try:
        with engine.begin() as connection:
            for report_id, expires_at, invalidated_at in legacy_rows:
                connection.execute(sa.text(
                    "INSERT INTO integration_analysis_reports "
                    "(id, organization_id, owner_user_id, membership_id, source_grant_id, "
                    "job_id, job_version_id, title, content_json, version, created_at, "
                    "updated_at, expires_at, invalidated_at, invalidation_reason) "
                    "VALUES (:id, 'synthetic-org', 'synthetic-user', 'synthetic-member', "
                    "'synthetic-grant', NULL, NULL, 'Synthetic legacy draft', "
                    "'{\"source\":\"test\"}', 1, '2025-01-01 00:00:00', "
                    "'2025-01-01 00:00:00', :expires_at, :invalidated_at, NULL)"
                ), {
                    "id": report_id,
                    "expires_at": expires_at,
                    "invalidated_at": invalidated_at,
                })
            before = connection.execute(sa.text(
                "SELECT * FROM integration_analysis_reports ORDER BY id"
            )).all()

        before_columns = {column["name"] for column in sa.inspect(engine).get_columns("integration_analysis_reports")}
        with pytest.raises(RuntimeError, match="integration_confirmation_legacy_reports_require_review"):
            command.upgrade(config, "20261001_0066")

        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "20260928_0065"
            after = connection.execute(sa.text(
                "SELECT * FROM integration_analysis_reports ORDER BY id"
            )).all()
        after_columns = {column["name"] for column in sa.inspect(engine).get_columns("integration_analysis_reports")}
        assert after == before
        assert after_columns == before_columns
        assert not {"source_credential_id", "source_audience", "source_auth_session_version", "confirmed_at"} & after_columns
    finally:
        engine.dispose()


def test_confirmation_migration_rejects_offline_sql_without_emitting_ddl(monkeypatch) -> None:
    migration = importlib.import_module(CONFIRMATION_MIGRATION)

    def no_database_access():
        pytest.fail("offline migration must not attempt a data preflight query")

    monkeypatch.setattr(migration, "op", SimpleNamespace(
        get_context=lambda: SimpleNamespace(as_sql=True),
        get_bind=no_database_access,
    ))
    with pytest.raises(RuntimeError, match="integration_confirmation_requires_online_preflight"):
        migration.upgrade()
