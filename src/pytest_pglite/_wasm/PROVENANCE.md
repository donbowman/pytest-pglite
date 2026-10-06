# Vendored WASM artifact provenance

`pytest-pglite` redistributes a prebuilt PostgreSQL 17.5 WebAssembly module so
that users never need a WebAssembly toolchain. This file records exactly what
is bundled and how to rebuild it.

## Bundled file

| File | Size | SHA-256 |
| --- | --- | --- |
| `pglite-17.5.20261006b.tar.xz` | 2,391,420 | `993458dd30e9e8989f5ed4c063a786ca89f7a89d7dd4d293a027051b7882fcff` |

The hash is also recorded in `SHA256SUMS` next to the artifact and is verified
on every first use.

## How it was built

The module is produced by this repository's build pipeline
(`build/scripts/build-wasi.sh`), which is a fork of
[electric-sql/pglite-build](https://github.com/electric-sql/pglite-build)
`portable` branch at commit
`c195113dbaf09488f8d5eeb2db91dacd123b74d0`, plus
`build/patches/0001-static-extensions.patch`:

- statically links pgvector 0.8.0 and installs `vector.control` /
  `vector--0.8.0.sql`
- statically links the PostgreSQL `pg_trgm` contrib module and installs
  `pg_trgm.control` plus the 1.3 install script and 1.3 -> 1.6 update scripts
- registers the SQL-visible symbols (including the explicit
  `AS 'MODULE_PATHNAME', 'symbol'` aliases) of every bundled extension in the
  WASI dlopen/dlsym shim
- calls each extension's `_PG_init` (renamed to avoid a duplicate symbol with
  plpgsql) so GUCs such as `hnsw.ef_search` and
  `pg_trgm.similarity_threshold` exist
- fixes the shim's one-element `dltab` array and off-by-one, which corrupted
  memory on the second `dlopen`
- clears `ActivePortal` during trap recovery so a simple-query error no longer
  wedges the only backend
- ships `lib/postgresql/{vector,pg_trgm}.so` placeholders (PostgreSQL checks
  that the library path exists before calling `dlopen`)
- bounds compiler parallelism through `PGLITE_JOBS` (upstream defaults to
  `nproc`)
- compiles the PostgreSQL core at `-O2`: the upstream WASI release leaves the
  core CFLAGS without an optimization flag (clang defaults to `-O0`), which
  makes CPU-bound SQL roughly 1.6-1.9x slower than the emscripten build

Build: 2026-10-05, Linux x86_64, wasi-sdk 25.0, PostgreSQL 17.5
(`REL_17_5_WASM`), pgvector 0.8.0, pg_trgm 1.6.

## Contents

~~~text
tmp/pglite/bin/pglite.wasi          PostgreSQL 17.5 + pgvector + pg_trgm (wasm32-wasip1), 23 MB
tmp/pglite/password                 superuser password ("password")
tmp/pglite/lib/postgresql/*.so      library placeholders (plpgsql, snowball, vector, pg_trgm)
tmp/pglite/share/postgresql/...     initdb inputs and extension SQL
~~~

## Rebuilding

See `build/README.md` in the repository root. After a successful build:

1. Boot the new module and run `pytest tests/test_vector.py tests/test_pg_trgm.py`
   against it (point the tests at it with `--pglite-wasm /path/to/tmp/pglite`).
2. Copy the tarball here under a dated filename, update `SHA256SUMS` and this
   file, and bump `ARTIFACT_FILENAME` in `src/pytest_pglite/artifacts.py`.

## Licences

PGlite is dual-licensed Apache-2.0 / PostgreSQL License. The postgres-pglite
changes, pgvector and pg_trgm are PostgreSQL License. libpglite is MIT. See
`THIRD_PARTY_NOTICES.md`.
