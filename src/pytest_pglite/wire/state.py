"""Per-connection protocol state and frontend message rewriting.

All client connections share one backend session, so prepared statement and
portal names must become globally unique. Names are rewritten on the wire;
backend response messages never contain statement or portal names, so the
mapping stays invisible to clients.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..protocol import (
    FE_BIND,
    FE_CLOSE,
    FE_DESCRIBE,
    FE_EXECUTE,
    FE_FLUSH,
    FE_PARSE,
    FE_QUERY,
    FE_SYNC,
    build_message,
    encode_cstring,
    parse_bind,
    parse_close,
    parse_describe,
    parse_execute,
    parse_parse,
    parse_query,
)

_COPY_FROM_STDIN_RE = re.compile(rb"(?is)\bcopy\b[^;]*\bfrom\s+stdin\b")


def is_copy_from_stdin(payload: bytes) -> bool:
    """Detect a simple-query COPY FROM STDIN statement."""
    try:
        sql = parse_query(payload)
    except Exception:  # noqa: BLE001 - malformed message, let the backend decide
        return False
    return bool(_COPY_FROM_STDIN_RE.search(sql))


_PIPELINE_MESSAGES = frozenset(
    {FE_PARSE, FE_BIND, FE_DESCRIBE, FE_EXECUTE, FE_CLOSE, FE_FLUSH}
)


@dataclass(eq=False)
class ConnState:
    """State for one client connection (identity based equality)."""

    cid: int
    pid: int = 0
    secret: int = 0
    statements: dict[bytes, bytes] = field(default_factory=dict)
    portals: dict[bytes, bytes] = field(default_factory=dict)
    unnamed_statement: bytes | None = None
    unnamed_portal: bytes | None = None
    statement_counter: int = 0
    portal_counter: int = 0
    txn_status: str = "I"
    pipeline_open: bool = False

    def new_statement_name(self) -> bytes:
        self.statement_counter += 1
        return f"pp{self.cid}s{self.statement_counter}".encode("ascii")

    def new_portal_name(self) -> bytes:
        self.portal_counter += 1
        return f"pp{self.cid}p{self.portal_counter}".encode("ascii")

    def lookup_statement(self, name: bytes) -> bytes:
        mapped = self.statements.get(name)
        if mapped is not None:
            return mapped
        if not name and self.unnamed_statement is not None:
            return self.unnamed_statement
        return name

    def lookup_portal(self, name: bytes) -> bytes:
        mapped = self.portals.get(name)
        if mapped is not None:
            return mapped
        if not name and self.unnamed_portal is not None:
            return self.unnamed_portal
        return name

    def on_error(self) -> None:
        """Mirror PostgreSQL: an error drops all portals and the unnamed statement."""
        self.portals.clear()
        self.unnamed_portal = None
        self.unnamed_statement = None

    def reset_after_restart(self) -> None:
        """Forget all names after the backend instance was replaced."""
        self.statements.clear()
        self.portals.clear()
        self.unnamed_statement = None
        self.unnamed_portal = None
        self.pipeline_open = False
        self.txn_status = "I"


def rewrite_frontend(state: ConnState, mtype: bytes, payload: bytes) -> bytes:
    """Rewrite a frontend message so it is safe on the shared backend."""
    if mtype == FE_SYNC:
        state.pipeline_open = False
    elif mtype in _PIPELINE_MESSAGES:
        state.pipeline_open = True
    if mtype == FE_PARSE:
        name, query, rest = parse_parse(payload)
        backend_name = state.new_statement_name()
        if name:
            state.statements[name] = backend_name
        else:
            state.unnamed_statement = backend_name
        return build_message(
            FE_PARSE, encode_cstring(backend_name) + encode_cstring(query) + rest
        )
    if mtype == FE_BIND:
        portal, statement, rest = parse_bind(payload)
        backend_statement = state.lookup_statement(statement)
        backend_portal = state.new_portal_name()
        if portal:
            state.portals[portal] = backend_portal
        else:
            state.unnamed_portal = backend_portal
        return build_message(
            FE_BIND,
            encode_cstring(backend_portal) + encode_cstring(backend_statement) + rest,
        )
    if mtype == FE_DESCRIBE:
        kind, name = parse_describe(payload)
        if kind == b"S":
            describe_target = state.lookup_statement(name)
        elif kind == b"P":
            describe_target = state.lookup_portal(name)
        else:
            describe_target = name
        return build_message(FE_DESCRIBE, kind + encode_cstring(describe_target))
    if mtype == FE_EXECUTE:
        portal, rest = parse_execute(payload)
        execute_portal = state.lookup_portal(portal)
        return build_message(FE_EXECUTE, encode_cstring(execute_portal) + rest)
    if mtype == FE_CLOSE:
        kind, name = parse_close(payload)
        close_target: bytes | None = None
        if kind == b"S":
            close_target = state.statements.get(name)
            state.statements.pop(name, None)
            if not name:
                close_target = state.unnamed_statement
                state.unnamed_statement = None
        else:
            close_target = state.portals.get(name)
            state.portals.pop(name, None)
            if not name:
                close_target = state.unnamed_portal
                state.unnamed_portal = None
        if close_target is None:
            close_target = name
        return build_message(FE_CLOSE, kind + encode_cstring(close_target))
    if mtype == FE_QUERY:
        # Extended-protocol state does not survive a simple query; keep the
        # unnamed statement mapping, exactly like PostgreSQL does not.
        return build_message(mtype, payload)
    return build_message(mtype, payload)


__all__ = ["ConnState", "is_copy_from_stdin", "rewrite_frontend"]
