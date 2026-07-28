from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(
        self,
        database_url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        default_organization_id: str | None = None,
    ) -> None:
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        engine_options: dict[str, object] = {
            "connect_args": connect_args,
            "pool_pre_ping": True,
        }
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            engine_options["poolclass"] = StaticPool
        elif not database_url.startswith("sqlite"):
            engine_options["pool_size"] = pool_size
            engine_options["max_overflow"] = max_overflow
        self.engine = create_engine(database_url, **engine_options)
        if database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)
        session_options: dict[str, object] = {
            "bind": self.engine,
            "autocommit": False,
            "autoflush": False,
            "expire_on_commit": False,
        }
        if default_organization_id:
            # Session ``info`` is copied per Session by SQLAlchemy.  The web
            # app uses the empty system workspace in production; the explicit
            # no-auth test harness keeps its historical single-workspace
            # fixture semantics without weakening production defaults.
            session_options["info"] = {
                "greatsell_organization_id": default_organization_id,
            }
        self.session_factory = sessionmaker(
            **session_options,
        )

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def get_session(request: Request) -> Generator[Session, None, None]:
    database: Database = request.app.state.database
    session = database.session_factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
