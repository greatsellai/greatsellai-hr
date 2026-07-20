from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_alembic_history_has_one_canonical_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))

    assert script.get_heads() == ["20260720_0022"]
