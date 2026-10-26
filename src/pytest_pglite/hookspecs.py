"""Hook specifications exposed by pytest-pglite.

A suite implements :func:`pytest_pglite_configure` in ``conftest.py`` to adjust
the configuration before any fixture starts a server, for example to install a
``prepare`` callable::

    def pytest_pglite_configure(pglite_config):
        pglite_config.prepare = migrate
        pglite_config.prepare_key = "alembic:0056"
"""

from __future__ import annotations

import pytest

from .config import PGliteConfig


@pytest.hookspec
def pytest_pglite_configure(pglite_config: PGliteConfig) -> None:
    """Adjust the PGlite configuration before the session fixtures run.

    The object is the validated configuration built from command-line options
    and ``PGLITE_*`` environment variables; mutate its fields in place.
    """
