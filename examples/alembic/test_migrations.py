"""Run Alembic migrations against PGlite."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

HERE = Path(__file__).parent


def test_upgrade_and_downgrade(pglite_url: str, pglite_engine: object) -> None:
    os.environ["PGLITE_URL"] = pglite_url
    config = Config(str(HERE / "alembic.ini"))
    config.set_main_option("script_location", str(HERE / "migrations"))

    command.upgrade(config, "head")
    with pglite_engine.connect() as connection:
        rows = connection.execute(
            text(
                "select column_name from information_schema.columns "
                "where table_name = 'accounts' order by ordinal_position"
            )
        ).all()
    assert [row[0] for row in rows] == ["id", "email"]

    command.downgrade(config, "base")
    with pglite_engine.connect() as connection:
        exists = connection.execute(
            text(
                "select count(*) from information_schema.tables "
                "where table_name = 'accounts'"
            )
        ).scalar_one()
    assert exists == 0
