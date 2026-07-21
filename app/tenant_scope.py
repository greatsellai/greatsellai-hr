"""Database-session workspace boundaries for recruiting business data.

The authenticated request selects a workspace on the server, then records it
on the SQLAlchemy session.  Every ORM read of a tenant-owned root is filtered
by that value and every write is stamped or verified before it reaches the
database.  This gives services a defence-in-depth boundary even when a future
endpoint forgets to add a manual ``organization_id`` predicate.

The legacy workspace is deliberately the safe fallback for unscoped internal
sessions.  It preserves existing service-level tests and, more importantly,
means an accidentally unscoped query can see only legacy records rather than
all customers' data.  Public requests always receive an explicit scope from
the authentication dependency.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, TYPE_CHECKING

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

if TYPE_CHECKING:
    from collections.abc import Generator


# Keep these stable IDs aligned with the data-only migration.  They are not a
# credential and let pre-registration records retain a safe, deterministic
# owner without moving source files or inspecting their contents.
LEGACY_ORGANIZATION_ID = "00000000-0000-4000-8000-000000000001"

_ORGANIZATION_ID_KEY = "greatsell_organization_id"
_BYPASS_KEY = "greatsell_skip_organization_scope"
_INSTALLED_KEY = "greatsell_tenant_scope_installed"


class TenantScopeError(RuntimeError):
    """Raised when code attempts a cross-workspace write."""


def set_organization_context(session: Session, organization_id: str) -> None:
    """Bind a session to one verified workspace.

    The caller must derive ``organization_id`` from a verified membership or
    a claimed worker job, never from a browser-controlled request field.
    """

    if not organization_id:
        raise TenantScopeError("organization_context_required")
    session.info[_ORGANIZATION_ID_KEY] = organization_id
    session.info.pop(_BYPASS_KEY, None)


def clear_organization_context(session: Session) -> None:
    """Remove an explicit scope after a worker unit of work completes."""

    session.info.pop(_ORGANIZATION_ID_KEY, None)
    session.info.pop(_BYPASS_KEY, None)


def organization_context_id(session: Session) -> str:
    """Return the current safe workspace, falling back only to legacy data."""

    return str(session.info.get(_ORGANIZATION_ID_KEY) or LEGACY_ORGANIZATION_ID)


def enable_organization_scope_bypass(session: Session) -> None:
    """Allow a short-lived system-only global worker claim query.

    Callers must immediately restore a concrete scope before loading linked
    business records or writing user-visible data.  This is intentionally not
    exposed through the HTTP layer.
    """

    session.info[_BYPASS_KEY] = True


def disable_organization_scope_bypass(session: Session) -> None:
    session.info.pop(_BYPASS_KEY, None)


@contextmanager
def bypass_organization_scope(session: Session) -> Iterator[None]:
    """Temporarily opt a trusted worker claim out of automatic filtering."""

    previous = session.info.get(_BYPASS_KEY)
    session.info[_BYPASS_KEY] = True
    try:
        yield
    finally:
        if previous:
            session.info[_BYPASS_KEY] = previous
        else:
            session.info.pop(_BYPASS_KEY, None)


def install_tenant_scope() -> None:
    """Install the ORM guards once, after ``OrganizationScoped`` is defined."""

    if getattr(Session, _INSTALLED_KEY, False):
        return

    # Imported lazily so ``app.models`` can call this function at the end of
    # its module without an import cycle.
    from app.models import CandidateDataLifecycle, OrganizationScoped

    @event.listens_for(Session, "do_orm_execute")
    def _apply_organization_criteria(execute_state: object) -> None:
        if not getattr(execute_state, "is_select", False):
            return
        execution_options = getattr(execute_state, "execution_options", {})
        session = getattr(execute_state, "session")
        if (
            execution_options.get("skip_organization_scope")
            or session.info.get(_BYPASS_KEY)
        ):
            return

        organization_id = organization_context_id(session)
        # ``track_closure_variables=False`` is safe here: the value is bound
        # as a SQL parameter, while the SQL shape remains cacheable.
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                OrganizationScoped,
                lambda entity: entity.organization_id == organization_id,
                include_aliases=True,
                propagate_to_loaders=True,
                track_closure_variables=False,
            )
        )
        # Candidate and Resume are the privacy roots.  Their derived records
        # stay in the database during a recovery window, but every ordinary
        # request must behave as if the deleted roots no longer exist.  The
        # narrow ``include_deleted_candidate_data`` option is reserved for
        # lifecycle restore/purge services; it is never set by an HTTP
        # request parameter.
        if not execution_options.get("include_deleted_candidate_data"):
            execute_state.statement = execute_state.statement.options(
                with_loader_criteria(
                    CandidateDataLifecycle,
                    lambda entity: entity.deleted_at.is_(None),
                    include_aliases=True,
                    propagate_to_loaders=True,
                )
            )

    @event.listens_for(Session, "before_flush")
    def _enforce_organization_writes(session: Session, *_: object) -> None:
        if session.info.get(_BYPASS_KEY):
            return
        expected_organization_id = organization_context_id(session)
        for instance in tuple(session.new) + tuple(session.dirty):
            if not isinstance(instance, OrganizationScoped):
                continue
            actual_organization_id = getattr(instance, "organization_id", None)
            if actual_organization_id is None:
                setattr(instance, "organization_id", expected_organization_id)
            elif actual_organization_id != expected_organization_id:
                raise TenantScopeError("cross_organization_write_rejected")

    setattr(Session, _INSTALLED_KEY, True)
