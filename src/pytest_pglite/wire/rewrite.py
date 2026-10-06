"""Statement rewrites for SQL features the single-backend build cannot run.

``CREATE INDEX CONCURRENTLY`` and its relatives cannot run on the single-user
WASI backend: the concurrent build performs several internal transactions and
the reduced transaction machinery aborts with ``tuple concurrently updated``
(upstream PGlite issue 901).  With one backend there are no concurrent
writers, so the CONCURRENTLY guarantee is meaningless; rewriting the statement
to the plain form keeps migration frameworks that rely on it working.

The rewriter only touches the keyword when it appears directly after the
statement's command, so occurrences inside strings, identifiers, comments and
dollar-quoted bodies are left alone.
"""

from __future__ import annotations

import re

_CONCURRENT_INDEX_PATTERNS: tuple[re.Pattern[bytes], ...] = (
    re.compile(rb"^(CREATE\s+(?:UNIQUE\s+)?INDEX)\s+CONCURRENTLY\b", re.IGNORECASE),
    re.compile(rb"^(DROP\s+INDEX)\s+CONCURRENTLY\b", re.IGNORECASE),
    re.compile(
        rb"^(REINDEX\s+(?:\([^)]*\)\s*)?(?:INDEX|TABLE|SCHEMA|DATABASE|SYSTEM))"
        rb"\s+CONCURRENTLY\b",
        re.IGNORECASE,
    ),
)

_WHITESPACE = b" \t\r\n\f\v"


def _iter_statements(sql: bytes) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` offsets of every top-level statement.

    Semicolons inside single quotes, double quotes, dollar-quoted strings,
    line comments and (nested) block comments do not terminate a statement.
    """
    bounds: list[tuple[int, int]] = []
    start = 0
    i = 0
    length = len(sql)
    while i < length:
        byte = sql[i]
        if byte in (0x27, 0x22):  # ' or "
            quote = byte
            i += 1
            while i < length:
                if sql[i] == quote:
                    if i + 1 < length and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if byte == 0x24:  # $, possibly a dollar-quoted string
            match = re.match(rb"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if match:
                tag = match.group(0)
                end = sql.find(tag, i + len(tag))
                i = length if end < 0 else end + len(tag)
                continue
        if byte == 0x2D and i + 1 < length and sql[i + 1] == 0x2D:  # --
            end = sql.find(b"\n", i)
            i = length if end < 0 else end + 1
            continue
        if byte == 0x2F and i + 1 < length and sql[i + 1] == 0x2A:  # /*
            depth = 1
            i += 2
            while i < length and depth:
                if sql[i : i + 2] == b"/*":
                    depth += 1
                    i += 2
                elif sql[i : i + 2] == b"*/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        if byte == 0x3B:  # ;
            bounds.append((start, i))
            i += 1
            start = i
            continue
        i += 1
    if start < length:
        bounds.append((start, length))
    return bounds


def _leading_trivia_length(statement: bytes) -> int:
    """Length of the whitespace and comments before the first keyword."""
    i = 0
    length = len(statement)
    while i < length:
        if statement[i] in _WHITESPACE:
            i += 1
            continue
        if statement[i : i + 2] == b"--":
            end = statement.find(b"\n", i)
            i = length if end < 0 else end + 1
            continue
        if statement[i : i + 2] == b"/*":
            depth = 1
            i += 2
            while i < length and depth:
                if statement[i : i + 2] == b"/*":
                    depth += 1
                    i += 2
                elif statement[i : i + 2] == b"*/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        break
    return i


def _rewrite_statement(statement: bytes) -> bytes:
    lead = _leading_trivia_length(statement)
    body = statement[lead:]
    for pattern in _CONCURRENT_INDEX_PATTERNS:
        match = pattern.match(body)
        if match is not None:
            return (
                statement[:lead]
                + body[: match.start(1)]
                + match.group(1)
                + body[match.end() :]
            )
    return statement


def rewrite_concurrent_index(sql: bytes) -> bytes:
    """Drop ``CONCURRENTLY`` from top-level index statements."""
    if b"concurrent" not in sql.lower():
        return sql
    pieces: list[bytes] = []
    last = 0
    for start, end in _iter_statements(sql):
        pieces.append(sql[last:start])
        pieces.append(_rewrite_statement(sql[start:end]))
        last = end
    pieces.append(sql[last:])
    return b"".join(pieces)


__all__ = ["rewrite_concurrent_index"]
