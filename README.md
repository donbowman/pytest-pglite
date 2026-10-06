# pytest-pglite

Real PostgreSQL in your Python process, for tests. No Docker, no server, no
Node.js. `pytest-pglite` embeds PGlite, the WebAssembly build of PostgreSQL
17.5, through [Wasmtime](https://wasmtime.dev/) and exposes it as a Postgres
wire-protocol server so every Python client just works.

```python
def test_users(pglite_engine):
    with pglite_engine.begin() as conn:
        conn.execute(text("create table users (id serial primary key, name text)"))
        conn.execute(text("insert into users (name) values ('Alice')"))
        assert conn.execute(text("select count(*) from users")).scalar_one() == 1
```

## Features

- **Native WASM** - PostgreSQL runs inside the Python process via Wasmtime,
  with only the standard WebAssembly System Interface as a dependency.
- **Real wire protocol** - psycopg (sync and async), asyncpg, SQLAlchemy,
  Django and anything else that speaks Postgres connect to a local Unix socket
  or TCP port.
- **pytest first** - session-scoped fast startup, function-scoped isolation,
  first-class support for `pytest-xdist` (each worker gets its own database).
- **Multiple connections** - connections share one single-user Postgres
  backend through a queue with transaction pinning, mirroring upstream
  PGlite behaviour.
- **Async ready** - the server is asyncio based; async fixtures are provided
  for FastAPI, httpx and other async test suites.
- **pgvector** - `CREATE EXTENSION vector` works out of the box.

- **pg_trgm** - `CREATE EXTENSION pg_trgm` works out of the box, with the
  `similarity()` functions, the `%` operator and GIN/GiST trigram indexes.
- **Common extensions included** - `hstore`, `citext`, `ltree`, `pg_trgm`,
  `btree_gin`, `btree_gist`, `unaccent`, `earthdistance` and more are
  bundled; see the list below.
- **Real error handling** - SQL errors abort the transaction, not the
  process: sessions keep running with the usual SQLSTATEs.
- **Fast** - a warm worker starts in well under a second; per-test isolation
  resets take milliseconds.

## Install

```bash
pip install pytest-pglite
# optional extras
pip install "pytest-pglite[sqlalchemy]"
pip install "pytest-pglite[async]"
pip install "pytest-pglite[django]"
pip install "pytest-pglite[examples]"
```

The pytest plugin is registered automatically through the `pytest11` entry
point, so there is nothing to import or configure.

## Fixtures

| Fixture | Scope | Description |
| --- | --- | --- |
| `pglite_server` | session | Running `PGliteServer` instance (one per xdist worker) |
| `pglite_dsn` | session | libpq connection string (Unix socket, or TCP) |
| `pglite_url` | session | SQLAlchemy URL string |
| `pglite_conn` | function | `psycopg.Connection` with a freshly reset schema |
| `pglite_async_conn` | function | `psycopg.AsyncConnection` with a freshly reset schema |
| `pglite_asyncpg` | function | `asyncpg.Connection` with a freshly reset schema |
| `pglite_engine` | function | SQLAlchemy `Engine` with a freshly reset schema |
| `pglite_execute` | function | Callable that runs SQL and returns rows |

## Options

```
--pglite-extensions=vector        Comma separated extensions to create
--pglite-isolation=schema         none | schema | transaction | database
--pglite-tcp                      Also listen on TCP (127.0.0.1, ephemeral port)
--pglite-tcp-address=host:port    Explicit TCP bind address (default port 0 = OS assigned)
--pglite-work-dir=PATH            Directory for database files (default: temp dir)
--pglite-wasm=PATH                Use a custom pglite.wasi build
--pglite-keep-tmp                 Keep the temporary database directory
--pglite-log-level=INFO           Engine log level
```

All options can also be set with environment variables using the `PGLITE_`
prefix, for example `PGLITE_ISOLATION=transaction`.

## Standalone server

The same engine can run outside pytest, for example for a manual psql session
or a development server:

```bash
pytest-pglite --tcp-address 127.0.0.1:0 --extensions vector
# PGlite ready: postgresql://postgres:password@127.0.0.1:54321/template1?sslmode=disable
```

```python
from pytest_pglite import PGliteConfig, PGliteServer

with PGliteServer(PGliteConfig(tcp=True)) as server:
    print(server.dsn)
```

## xdist

Each `pytest-xdist` worker process starts its own PGlite instance with its own
temporary directory, its own Unix socket (or its own OS-assigned TCP port).
There are no shared paths and no port collisions, so `pytest -n 4` just works.

## How it works

The package embeds `pglite.wasi`, a WebAssembly build of PostgreSQL 17.5 with
pgvector and a set of contrib extensions statically linked, and with
PostgreSQL's normal `setjmp`/`longjmp` error handling enabled. It is built
from [electric-sql/pglite-build](https://github.com/electric-sql/pglite-build)
with the newest wasi-sdk and Binaryen by the pipeline in `build/` (see
`build/README.md` for the patches and provenance). A small engine wrapper
drives the WASI module through Wasmtime.
On top of the
single-user backend, an asyncio Postgres wire-protocol proxy manages client
connections: each connection is a logical session, and protocol exchanges are
serialised through a query queue. Statements and portals are renamed per
connection so prepared-statement clients (psycopg3, asyncpg, SQLAlchemy) can
share one backend safely.

PGlite is single user, exactly like embedded SQLite. Concurrent connections
work, but work is serialised; a connection holding an open transaction holds
the backend until it commits or rolls back.

## Performance

Measured on the development machine (Linux x86_64, CPython 3.14):

| Measurement | Result |
| --- | --- |
| Cold start (first ever, compiles WASM + initdb) | about 2.5 s |
| Warm start, new worker (AOT + template cache) | about 0.25 s |
| New connection | about 1 ms |
| Query round trip (`pytest-pglite` overhead) | about 1 ms |
| Aggregate over 200,000 rows | about 0.03 s |
| Per-test schema reset | about 1 ms |

Compiled-module files and an initialised database template are cached under
the platform cache directory; `pytest-xdist` workers share them.

## Limitations

- Single database, single user (`postgres`). `CREATE DATABASE` and connected
  database switching are not supported.
- Single backend: work from concurrent connections is queued, and a connection
  holding an open transaction blocks others until it commits or rolls back.
  Waiting connections time out with SQLSTATE 57014 after `queue_timeout`
  seconds (default 60).
- Session state is shared: `SET search_path`, `SET timezone` and similar
  run-time parameters affect every connection because they share one backend
  session. Use `SET LOCAL` inside a transaction for per-test values.
- PostgreSQL wire-protocol server features are limited to what test suites
  need: `LISTEN/NOTIFY` delivery is not implemented, and cancellation requests
  are accepted but not acted on.

- `CREATE INDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY` and `REINDEX ...
  CONCURRENTLY` are accepted for compatibility. The single-backend WASI port
  cannot run the concurrent build's internal transaction machinery (upstream
  [PGlite #901](https://github.com/electric-sql/pglite/issues/901)), so the
  proxy rewrites the statement to its plain form; with one backend there are
  no concurrent writers, and an `IF NOT EXISTS` statement still
  short-circuits when the index exists. Disable the rewrite with
  `--pglite-no-concurrent-index-rewrite` or
  `PGLITE_REWRITE_CONCURRENT_INDEX=false`.
- `COPY TO STDOUT` works; `COPY FROM STDIN` is rejected with SQLSTATE 0A000
  because libpglite's synchronous transport cannot stream `CopyData` during
  `CopyFrom`.
- SQL errors use PostgreSQL's own error recovery: the ErrorResponse and
  SQLSTATE are forwarded and the backend keeps running, so transactions abort
  and sessions continue exactly as in a normal server. The Python host keeps a
  trap-recovery fallback (`PGliteServer.trap_count`) for unexpected
  WebAssembly traps.
- Extensions are compiled into the WASM module at build time. The bundled set
  is pgvector plus `bloom`, `btree_gin`, `btree_gist`, `citext`, `cube`,
  `dict_int`, `earthdistance`, `fuzzystrmatch`, `hstore`, `intarray`, `isn`,
  `ltree`, `pg_trgm`, `seg`, `tablefunc`, `tsm_system_rows`,
  `tsm_system_time` and `unaccent`.
- No Windows support for Unix sockets; use `--pglite-tcp` on Windows.

## Development

```bash
poetry install --all-extras
poetry run pytest
poetry run black .
poetry run flake8
poetry run mypy
```

The WASM artifact lives in `src/pytest_pglite/_wasm/`; see `build/README.md`
for provenance and how to rebuild it. Rebuilding uses the upstream
pglite-build pipeline; the orchestrator pins compiler parallelism through
`PGLITE_JOBS` (default 8) because upstream defaults to `nproc` and can exhaust
memory on large machines.

## License

Apache License 2.0. See [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
