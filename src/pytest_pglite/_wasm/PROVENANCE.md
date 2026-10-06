# Vendored WASM artifact provenance

`pytest-pglite` redistributes a prebuilt PostgreSQL 17.5 WebAssembly module so
that users never need a WebAssembly toolchain. This file records exactly what
is bundled and how to rebuild it.

## Bundled file

| File | Size | SHA-256 |
| --- | --- | --- |
| `pglite-17.5.20261006.tar.xz` | 5,145,000 | `cea663a6ca74ee8183f223e4a7c2255fe5a86cb4c491545669f95cc9e9370488` |

The hash is also recorded in `SHA256SUMS` next to the artifact and is verified
on every first use.

## How it was built

The module is produced by this repository's build pipeline
(`build/scripts/build-wasi.sh`), which is a fork of
[electric-sql/pglite-build](https://github.com/electric-sql/pglite-build)
`portable` branch at commit
`c195113dbaf09488f8d5eeb2db91dacd123b74d0`, plus
`build/patches/0001-vector-static.patch`:

- statically links pgvector 0.8.0 and installs `vector.control` /
  `vector--0.8.0.sql`
- registers the 104 SQL-visible pgvector symbols (including the 16 explicit
  `AS 'MODULE_PATHNAME', 'symbol'` aliases) in the WASI dlopen/dlsym shim
- calls pgvector's `_PG_init` (renamed to avoid a duplicate symbol with
  plpgsql) so `hnsw.ef_search` and the other GUCs exist
- fixes the shim's one-element `dltab` array and off-by-one, which corrupted
  memory on the second `dlopen`
- clears `ActivePortal` during trap recovery so a simple-query error no longer
  wedges the only backend
- ships a `lib/postgresql/vector.so` placeholder (PostgreSQL checks that the
  library path exists before calling `dlopen`)
- bounds compiler parallelism through `PGLITE_JOBS` (upstream defaults to
  `nproc`)

Build: 2026-10-06, Linux x86_64, wasi-sdk 25.0, PostgreSQL 17.5
(`REL_17_5_WASM`), pgvector 0.8.0.

## Contents

~~~text
tmp/pglite/bin/pglite.wasi          PostgreSQL 17.5 + pgvector (wasm32-wasip1), 23 MB
tmp/pglite/password                 superuser password ("password")
tmp/pglite/lib/postgresql/*.so      library placeholders (plpgsql, snowball, vector)
tmp/pglite/share/postgresql/...     initdb inputs and extension SQL
~~~

## Rebuilding

See `build/README.md` in the repository root. After a successful build:

1. Boot the new module and run `pytest tests/test_vector.py` against it (point
   the tests at it with `--pglite-wasm /path/to/tmp/pglite`).
2. Copy the tarball here under a dated filename, update `SHA256SUMS` and this
   file, and bump `ARTIFACT_FILENAME` in `src/pytest_pglite/artifacts.py`.

## Licences

PGlite is dual-licensed Apache-2.0 / PostgreSQL License. The postgres-pglite
changes and pgvector are PostgreSQL License. libpglite is MIT. See
`THIRD_PARTY_NOTICES.md`.
