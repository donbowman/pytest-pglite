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
--pglite-rebuild-templates        Ignore and replace cached database templates
--pglite-log-level=INFO           Engine log level
--pglite-restart-every=N          Restart the instance every N tests (see below)
--pglite-heap-restart-mb=MB       Restart the instance when its guest heap exceeds MB
--pglite-no-oom-restart           Do not restart automatically after an out of memory
--pglite-memory-reservation-mb=MB Address space reserved per guest memory
```

All options can also be set with environment variables using the `PGLITE_`
prefix, for example `PGLITE_ISOLATION=transaction`.

## Long runs and the guest heap

PGlite runs PostgreSQL inside a 32-bit WebAssembly linear memory. That memory
can **grow but never shrink**, so the guest heap only ever creeps upwards for
the life of an instance, and a long enough session eventually reaches the
ceiling (about 4 GiB). When it does, PostgreSQL reports SQLSTATE `53200` for an
ordinary small allocation, and from then on *every* query fails with a
confusing "out of memory" even though the host has RAM to spare.

Measured on a real suite (one instance, small tests, a schema reset per test):
the heap climbs by roughly 2.5 MiB per test, so a few thousand tests reach the
ceiling. Nothing in PostgreSQL gives the memory back; the only recovery is to
replace the instance, which `pytest-pglite` can do over the same data directory.

```bash
pytest --pglite-restart-every=500          # bound it by test count
pytest --pglite-heap-restart-mb=1500       # or by heap size, checked between tests
```

Both keep the data; the cost is a fresh backend session, so prepared statements
and session state are gone (the next test reconnects). A restart is only taken
between tests, and only when no connection holds the backend. Set
`PGLITE_RESTART_EVERY_N_TESTS` or `PGLITE_HEAP_RESTART_MB` in a test
environment to make it the default for a whole suite.

Independently of any policy, an out-of-memory error triggers one automatic
restart so a single exhausted test does not fail the rest of the run
(`--pglite-no-oom-restart` turns that off, `oom_restart_limit` bounds it).

The library also reads and reports the heap, which is the number to watch:

```python
server.heap_bytes        # current guest heap, bytes
server.heap_limit_bytes  # the artifact's declared ceiling, or None (unbounded)
server.restarts          # policy restarts so far
server.oom_restarts      # restarts caused by an out-of-memory error
server.restart("reason") # replace the instance, keeping the data
```

Three further things help a long run, and all are on by default:

- **Session settings tuned for one WASM backend**: `maintenance_work_mem=16MB`,
  `max_parallel_workers_per_gather=0`, `max_parallel_maintenance_workers=0` and
  `jit=off`. Parallel workers cannot run in parallel here, and a 64 MB
  maintenance_work_mem is a large transient spike for an index build. Override
  or drop any of them, per suite, with `session_settings` (a value of `''`
  drops a default):

  ```python
  PGliteConfig(session_settings={"maintenance_work_mem": "32MB", "jit": ""})
  ```

- **A host memory configuration** for many instances at once. By default
  Wasmtime reserves a large amount of *virtual* address space per instance
  (measured at 16.5 GiB, with only about 0.6 GiB resident), so `pytest -n 8`
  reserves over 100 GiB. Setting the reservation and the growth reservation
  cuts that roughly in half, measured on one instance:

  | Configuration | Virtual address space |
  | --- | --- |
  | default | 16.5 GiB |
  | `memory_reservation_mb=1024` | 10.5 GiB |
  | `memory_reservation_mb=1024`, `memory_reservation_for_growth_mb=1024` | 10.5 GiB |
  | `memory_reservation_mb=512`, `memory_reservation_for_growth_mb=512` | 9.5 GiB |
  | `memory_reservation_mb=1`, `memory_reservation_for_growth_mb=1` | 8.6 GiB |

  A very small reservation also means frequent relocation when the memory
  grows, so pick a reservation comfortably above the heap a run reaches
  (the restart policy keeps that bounded). `memory_guard_size_mb` is exposed
  too but barely moves the total (16.5 to 16.4 GiB measured). This is the
  modern replacement for `static_memory_maximum_size(0)`, which the current
  bindings no longer expose. **None of it changes the guest ceiling**: the
  32-bit address space is the limit, growth still works in every configuration
  above, and only a restart reclaims the heap.

- **The heap size in the log**: the startup line reports the heap and the
  declared ceiling, and an out-of-memory restart logs the heap it hit.

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

## Seeding an application schema

A suite that runs migrations can build them once and publish the resulting data
directory, so every xdist worker starts from a copy instead of each worker
migrating at session start:

```python
# conftest.py
import psycopg

from pytest_pglite import IsolationMode


def migrate(server):
    with psycopg.connect(server.dsn) as connection:
        ...  # Alembic, SQLAlchemy or plain SQL
        connection.commit()


def pytest_pglite_configure(pglite_config):
    pglite_config.prepare = migrate
    pglite_config.prepare_key = "alembic:0056"
    pglite_config.isolation = IsolationMode.NONE
```

`prepare` runs once per cache key on a fresh server, with `server.dsn`,
`server.url` and `server.execute` usable while it runs. The guest is then shut
down cleanly so the directory is checkpointed, copied into the template cache
and renamed into place. Workers that raced the builder wait on a `flock` and
then copy the finished template; if the builder dies the kernel releases the
lock, so the rest of the run continues. `prepare_key` is the caller's schema
revision and must change whenever the migrations change. It is mixed with the
artifact digest and the extension list, so a PostgreSQL bump or a schema change
misses the cache instead of resuming the wrong catalog.

The default `schema` isolation drops and recreates `public` around every test,
which would remove the prepared application tables. Use `transaction` or `none`
isolation with `prepare`.

Templates live under the platform cache directory. `PGLITE_TEMPLATE_CACHE_MAX_MB`
(default 2048) caps their total size and the least recently published entries
are pruned; `0` disables pruning. `PGLITE_REBUILD_TEMPLATES=1` or
`--pglite-rebuild-templates` ignores and replaces them. Startup logs one line
with the cache key, whether it was a hit or a build, and the seconds. A corrupt
template, or one whose guest will not start, is rebuilt with a warning instead
of failing the session.

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

Compiled-module files and database templates are cached under
the platform cache directory; `pytest-xdist` workers share them. The initdb
template caches the cluster after extensions are created; the `prepare` hook
caches an application schema on top of it. Both are built once under a file
lock, even when every worker starts at the same moment.

## Limitations

- The guest heap only grows. PostgreSQL runs inside a 32-bit WebAssembly
  linear memory that is never returned to the host, so a long session
  eventually reaches the ceiling and fails small allocations with SQLSTATE
  53200. Set a restart policy (`--pglite-restart-every` or
  `--pglite-heap-restart-mb`) for a long suite; see
  [Long runs and the guest heap](#long-runs-and-the-guest-heap).
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
