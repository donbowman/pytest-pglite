"""Django test configuration for the PGlite example.

pytest-django would normally create a separate ``test_<name>`` database. The
single-user PGlite backend cannot switch databases, so ``django_db_setup`` is
replaced: tables are created in the configured database and each test runs in
the usual pytest-django transaction that is rolled back afterwards.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(scope="session")
def django_db_setup(django_db_blocker: Any) -> None:
    from django.core.management import call_command

    with django_db_blocker.unblock():
        call_command("migrate", run_syncdb=True, verbosity=0)
