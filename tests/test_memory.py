"""The guest heap: introspection, session tuning, and restart policy.

PGlite runs PostgreSQL inside a 32-bit WebAssembly linear memory. That memory
can grow but never shrink, so a long test session ratchets it upwards until an
allocation fails and every later query fails with a confusing "out of memory".
These tests cover the machinery that makes that visible and recoverable: the
heap reading, the WASM-tuned session settings, the restart policy, and the
out-of-memory recovery.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from pytest_pglite import (
    DEFAULT_SESSION_SETTINGS,
    PGliteConfig,
    PGliteServer,
    live_servers,
)
from pytest_pglite import plugin as plugin_module
from pytest_pglite.protocol import (
    BE_ERROR_RESPONSE,
    OUT_OF_MEMORY,
    build_message,
    scan_backend,
)
from pytest_pglite.wire.server import WireServer

MIB = 1 << 20


def error_response_payload(*, code: str, message: str) -> bytes:
    """A minimal ErrorResponse payload with the given SQLSTATE."""
    fields = [(b"S", b"ERROR"), (b"C", code.encode()), (b"M", message.encode())]
    return b"".join(tag + value + b"\x00" for tag, value in fields) + b"\x00"


# ----------------------------------------------------------------------
# protocol: recognising a heap exhaustion
# ----------------------------------------------------------------------
def test_scan_recognises_out_of_memory():
    reply = build_message(
        BE_ERROR_RESPONSE,
        error_response_payload(
            code=OUT_OF_MEMORY,
            message=(
                "out of memory\nDETAIL: Failed on request of size 1024 "
                'in memory context "MessageContext".'
            ),
        ),
    )
    scan = scan_backend(reply)
    assert scan.has_error
    assert scan.error_code == OUT_OF_MEMORY
    assert scan.is_out_of_memory


def test_scan_does_not_mistake_a_normal_error_for_out_of_memory():
    reply = build_message(
        BE_ERROR_RESPONSE,
        error_response_payload(code="42P01", message="relation does not exist"),
    )
    scan = scan_backend(reply)
    assert scan.has_error
    assert scan.error_code == "42P01"
    assert not scan.is_out_of_memory


def test_error_code_parsing_tolerates_a_missing_code():
    payload = b"S" + b"ERROR\x00" + b"M" + b"boom\x00" + b"\x00"
    assert scan_backend(build_message(BE_ERROR_RESPONSE, payload)).error_code is None


# ----------------------------------------------------------------------
# configuration
# ----------------------------------------------------------------------
def test_restart_policy_defaults_to_off():
    cfg = PGliteConfig()
    assert cfg.restart_every_n_tests is None
    assert cfg.heap_restart_mb is None
    assert cfg.restart_on_out_of_memory is True
    assert cfg.oom_restart_limit == 5
    assert cfg.session_settings == {}
    assert cfg.memory_reservation_mb is None
    assert cfg.memory_may_move is None


def test_restart_policy_reads_the_environment(monkeypatch):
    monkeypatch.setenv("PGLITE_RESTART_EVERY_N_TESTS", "250")
    monkeypatch.setenv("PGLITE_HEAP_RESTART_MB", "1500")
    monkeypatch.setenv("PGLITE_OOM_RESTART_LIMIT", "2")
    cfg = PGliteConfig()
    assert cfg.restart_every_n_tests == 250
    assert cfg.heap_restart_mb == 1500
    assert cfg.oom_restart_limit == 2


def test_session_settings_read_the_environment_as_json(monkeypatch):
    monkeypatch.setenv("PGLITE_SESSION_SETTINGS", '{"work_mem": "8MB"}')
    assert PGliteConfig().session_settings == {"work_mem": "8MB"}


@pytest.mark.parametrize("field", ["restart_every_n_tests", "heap_restart_mb"])
def test_restart_thresholds_must_be_positive(field: str):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PGliteConfig(**{field: 0})


def test_oom_restart_limit_may_be_zero_to_disable():
    assert PGliteConfig(oom_restart_limit=0).oom_restart_limit == 0


# ----------------------------------------------------------------------
# plugin wiring
# ----------------------------------------------------------------------
class StubConfig:
    """A stand-in for pytest.Config, carrying only pglite options."""

    def __init__(self, **options: Any) -> None:
        self._options = options

    def getoption(self, name: str) -> Any:
        return self._options.get(name, None)


def test_command_line_options_reach_the_config():
    stub = StubConfig(
        pglite_restart_every="100",
        pglite_heap_restart_mb="900",
        pglite_no_oom_restart=True,
        pglite_memory_reservation_mb="512",
    )
    cfg = plugin_module._config_from_options(stub)
    assert cfg.restart_every_n_tests == 100
    assert cfg.heap_restart_mb == 900
    assert cfg.restart_on_out_of_memory is False
    assert cfg.memory_reservation_mb == 512


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("pglite_restart_every", "many"),
        ("pglite_heap_restart_mb", "1.5"),
        ("pglite_memory_reservation_mb", "0"),
    ],
)
def test_bad_command_line_numbers_are_usage_errors(option: str, value: str):
    with pytest.raises(pytest.UsageError):
        plugin_module._config_from_options(StubConfig(**{option: value}))


def test_report_header_shows_the_restart_policy():
    stub = StubConfig(pglite_restart_every="50", pglite_heap_restart_mb="800")
    stub._pglite_config = plugin_module._config_from_options(stub)
    header = plugin_module.pytest_report_header(stub)
    assert header is not None
    assert "restart/50 tests" in header
    assert "restart above 800 MiB" in header


def test_report_header_says_when_there_is_no_policy():
    stub = StubConfig()
    stub._pglite_config = plugin_module._config_from_options(stub)
    header = plugin_module.pytest_report_header(stub)
    assert header is not None
    assert "no heap restart policy" in header


class RecordingServer:
    """Stands in for a PGliteServer in the lifecycle hook."""

    def __init__(self, due: str | None = None) -> None:
        self.counted = 0
        self.considered = 0
        self.due = due

    def note_test_finished(self) -> int:
        self.counted += 1
        return self.counted

    def consider_restart(self) -> str | None:
        self.considered += 1
        return self.due


def test_teardown_hook_counts_tests_and_applies_the_policy(monkeypatch):
    server = RecordingServer(due="20 tests since the last restart")
    monkeypatch.setattr(plugin_module, "live_servers", lambda: (server,))

    plugin_module.pytest_runtest_teardown(None, object())
    assert server.counted == 1
    assert server.considered == 1


def test_teardown_hook_skips_the_restart_after_the_last_test(monkeypatch):
    server = RecordingServer(due="20 tests since the last restart")
    monkeypatch.setattr(plugin_module, "live_servers", lambda: (server,))

    plugin_module.pytest_runtest_teardown(None, None)
    assert server.counted == 1
    # No point replacing the instance when the run is over.
    assert server.considered == 0


# ----------------------------------------------------------------------
# out-of-memory recovery (no database needed)
# ----------------------------------------------------------------------
class FakeEngine:
    def __init__(self, heap: int, limit: int | None) -> None:
        self.heap_bytes = heap
        self.heap_limit_bytes = limit


def make_wire_server(config: PGliteConfig, heap: int = 2 * 1024 * MIB) -> WireServer:
    engine = FakeEngine(heap=heap, limit=None)
    return WireServer(engine=engine, config=config)  # type: ignore[arg-type]


async def test_out_of_memory_restarts_the_instance(monkeypatch):
    server = make_wire_server(PGliteConfig())
    restarts: list[str] = []

    async def fake_restart(reason: str) -> None:
        restarts.append(reason)

    monkeypatch.setattr(server, "restart_engine", fake_restart)
    await server._recover_out_of_memory("out of memory")

    assert server.oom_restarts == 1
    assert len(restarts) == 1
    assert "guest heap exhausted" in restarts[0]
    assert "2048.0 MiB" in restarts[0]


async def test_out_of_memory_restart_can_be_disabled(monkeypatch):
    server = make_wire_server(PGliteConfig(restart_on_out_of_memory=False))
    restarts: list[str] = []

    async def fake_restart(reason: str) -> None:
        restarts.append(reason)

    monkeypatch.setattr(server, "restart_engine", fake_restart)

    await server._recover_out_of_memory("out of memory")

    assert server.oom_restarts == 0
    assert restarts == []


async def test_out_of_memory_restarts_stop_at_the_limit(monkeypatch):
    server = make_wire_server(PGliteConfig(oom_restart_limit=2))
    restarts: list[str] = []

    async def fake_restart(reason: str) -> None:
        restarts.append(reason)

    monkeypatch.setattr(server, "restart_engine", fake_restart)

    for _ in range(5):
        await server._recover_out_of_memory("out of memory")

    assert server.oom_restarts == 2
    assert len(restarts) == 2


# ----------------------------------------------------------------------
# integration: the real guest heap and the real session
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def server() -> Iterator[PGliteServer]:
    with PGliteServer(PGliteConfig(tcp=True)) as running:
        yield running


@pytest.mark.integration
def test_heap_is_readable_and_the_artifact_declares_no_ceiling(
    server: PGliteServer,
) -> None:
    assert server.heap_bytes > 0
    # The bundled artifact declares memory min=76 pages with no maximum, so the
    # ceiling is the 32-bit address space rather than a number in the artifact.
    assert server.heap_limit_bytes is None
    assert server.oom_restarts == 0


@pytest.mark.integration
def test_wasm_tuned_session_settings_are_applied(server: PGliteServer) -> None:
    assert server.execute("SHOW maintenance_work_mem") == [["16MB"]]
    assert server.execute("SHOW max_parallel_workers_per_gather") == [["0"]]
    assert server.execute("SHOW max_parallel_maintenance_workers") == [["0"]]
    assert server.execute("SHOW jit") == [["off"]]


@pytest.mark.integration
def test_session_settings_can_override_and_drop_defaults() -> None:
    config = PGliteConfig(
        tcp=True,
        session_settings={
            "maintenance_work_mem": "8MB",
            # '' drops a default rather than setting it, so the build's own
            # value (2) comes back.
            "max_parallel_workers_per_gather": "",
        },
    )
    with PGliteServer(config) as custom:
        assert custom.execute("SHOW maintenance_work_mem") == [["8MB"]]
        assert custom.execute("SHOW max_parallel_workers_per_gather") == [["2"]]
        # The untouched defaults still apply.
        assert custom.execute("SHOW jit") == [["off"]]


@pytest.mark.integration
def test_restart_keeps_the_data_and_reclaims_the_heap(server: PGliteServer) -> None:
    heap_at_start = server.heap_bytes

    # Grow the heap: a hundred thousand rows and an index build make the guest
    # allocate beyond its starting size.
    server.execute("DROP TABLE IF EXISTS heap_work")
    server.execute(
        "CREATE TABLE heap_work AS SELECT i AS id, md5(i::text) AS body "
        "FROM generate_series(1, 100000) AS i"
    )
    server.execute("CREATE INDEX heap_work_body_idx ON heap_work (body)")
    server.execute("ANALYZE heap_work")
    grown = server.heap_bytes
    # The workload must actually have moved the heap, or the rest of the test
    # would prove nothing.
    assert grown > heap_at_start

    assert server.restart("test asked for it") is True

    # The data survives...
    assert server.execute("SELECT count(*) FROM heap_work") == [["100000"]]
    # ...and the heap comes back to its starting size. Only a restart can do
    # that: without one it would still be at the grown size.
    assert server.heap_bytes <= heap_at_start + 8 * MIB
    # ...and the counters move.
    assert server.restarts == 1
    assert server.tests_since_restart == 0


@pytest.mark.integration
def test_restart_policy_counts_tests_and_triggers() -> None:
    config = PGliteConfig(tcp=True, restart_every_n_tests=3, heap_restart_mb=1 << 30)
    with PGliteServer(config) as policy:
        assert policy.restart_due() is None
        policy.note_test_finished()
        policy.note_test_finished()
        assert policy.restart_due() is None
        policy.note_test_finished()
        assert policy.restart_due() is not None

        reason = policy.consider_restart()
        assert reason is not None
        assert policy.restarts == 1
        assert policy.tests_since_restart == 0
        assert policy.restart_due() is None


@pytest.mark.integration
def test_restart_is_due_when_the_heap_exceeds_a_threshold() -> None:
    # The guest memory starts at a few MiB, so a 1 MiB threshold is already
    # exceeded, and the reason names the size so a log reader can see why.
    with PGliteServer(PGliteConfig(tcp=True, heap_restart_mb=1)) as probe:
        reason = probe.restart_due()
        assert reason is not None
        assert "guest heap" in reason


@pytest.mark.integration
def test_started_servers_are_registered_for_the_plugin(server: PGliteServer) -> None:
    assert server in live_servers()


@pytest.mark.integration
def test_memory_options_are_accepted_and_applied() -> None:
    config = PGliteConfig(
        tcp=True,
        memory_reservation_mb=64,
        memory_reservation_for_growth_mb=16,
        memory_guard_size_mb=1,
        memory_may_move=True,
        max_wasm_stack_mb=2,
    )
    with PGliteServer(config) as tuned:
        assert tuned.execute("SELECT 1") == [["1"]]
        assert tuned.heap_bytes > 0


# ----------------------------------------------------------------------
# engine internals that need no database
# ----------------------------------------------------------------------
def test_memory_option_names_missing_from_wasmtime_are_ignored(caplog) -> None:
    """An unknown knob must warn, not break startup (bindings differ by release)."""
    from pytest_pglite.engine import WasmEngine

    class FakeWasmtimeConfig:
        pass

    engine = WasmEngine.__new__(WasmEngine)
    engine._memory_options = {"static_memory_maximum_size": 0}
    engine._log = logging.getLogger("pytest_pglite.engine")
    with caplog.at_level(logging.WARNING):
        engine._apply_memory_options(FakeWasmtimeConfig())
    assert "has no static_memory_maximum_size option" in caplog.text


def test_default_session_settings_are_documented() -> None:
    assert DEFAULT_SESSION_SETTINGS["maintenance_work_mem"] == "16MB"
    assert DEFAULT_SESSION_SETTINGS["max_parallel_workers_per_gather"] == "0"
    assert DEFAULT_SESSION_SETTINGS["max_parallel_maintenance_workers"] == "0"
    assert DEFAULT_SESSION_SETTINGS["jit"] == "off"


def test_setting_name_validation_rejects_injection() -> None:
    """A setting name must be a GUC name, never SQL."""
    from pytest_pglite.engine import _SETTING_NAME_RE

    assert _SETTING_NAME_RE.match("maintenance_work_mem")
    assert _SETTING_NAME_RE.match("jit")
    assert not _SETTING_NAME_RE.match("work_mem = '8MB'; DROP TABLE users")
    assert not _SETTING_NAME_RE.match("1bad")


def test_setting_value_quoting() -> None:
    from pytest_pglite.engine import _quote_setting

    assert _quote_setting("16MB") == "'16MB'"
    assert _quote_setting("it's") == "'it''s'"


def test_session_settings_merge_over_the_defaults() -> None:
    """A host override wins, and '' drops a default."""
    from pytest_pglite.engine import WasmEngine

    engine = WasmEngine.__new__(WasmEngine)
    engine._session_setting_overrides = {
        "maintenance_work_mem": "8MB",
        "jit": "",
    }
    merged = engine._session_settings()
    assert merged["maintenance_work_mem"] == "8MB"
    assert "jit" not in merged
    assert merged["max_parallel_workers_per_gather"] == "0"
