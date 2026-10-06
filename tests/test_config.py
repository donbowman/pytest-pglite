"""Unit tests for configuration models."""

from __future__ import annotations

import pytest

from pytest_pglite.config import IsolationMode, PGliteConfig


def test_defaults() -> None:
    config = PGliteConfig()
    assert config.pg_version == "17.5"
    assert config.isolation is IsolationMode.SCHEMA
    assert config.extensions == ()
    assert config.tcp is False
    assert config.tcp_port == 0
    assert config.aot_cache is True


def test_extensions_are_normalised_and_deduplicated() -> None:
    config = PGliteConfig(extensions=("Vector", " vector ", "pg_trgm"))
    assert config.extensions == ("vector", "pg_trgm")


@pytest.mark.parametrize("bad", ["vector;drop", "1vector", "vector-x", ""])
def test_invalid_extension_names(bad: str) -> None:
    if bad == "":
        config = PGliteConfig(extensions=(bad,))
        assert config.extensions == ()
    else:
        with pytest.raises(ValueError):
            PGliteConfig(extensions=(bad,))


def test_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGLITE_ISOLATION", "transaction")
    monkeypatch.setenv("PGLITE_EXTENSIONS", "vector,pg_trgm")
    monkeypatch.setenv("PGLITE_TCP", "true")
    config = PGliteConfig()
    assert config.isolation is IsolationMode.TRANSACTION
    assert config.extensions == ("vector", "pg_trgm")
    assert config.tcp is True


def test_bad_log_level() -> None:
    with pytest.raises(ValueError):
        PGliteConfig(log_level="chatty")


def test_work_dir_is_expanded() -> None:
    config = PGliteConfig(work_dir="~/somewhere")
    assert config.work_dir is not None
    assert "~" not in str(config.work_dir)
