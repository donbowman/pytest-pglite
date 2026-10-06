# Vendored WASM artifact provenance

`pytest-pglite` redistributes a prebuilt PostgreSQL 17.5 WebAssembly module so
that users never need a WebAssembly toolchain. This file records exactly what
is bundled and how to rebuild it.

## Bundled files

| File | Size | SHA-256 |
| --- | --- | --- |
| `pglite-17.5.20250726.tar.xz` | 5,115,020 | `c725235f22a4fd50fed363f4065edb151a716fa769cba66f2383b8b854e6bdb5` |
| `pglite-prefix-17.5.tar.xz` | 139,142 | `751ff6f3101b3285023cf0f151690f24fb8b7db941211c075db4991668fa91c9` |

The hashes are also recorded in `SHA256SUMS` next to the artifacts and are
verified on every first use.

## Sources

- `pglite-17.5.20250726.tar.xz` is the WASI build published by the
  [electric-sql/pglite-build](https://github.com/electric-sql/pglite-build)
  project (`portable` branch) at
  <https://electric-sql.github.io/pglite-build/pglite-wasi.tar.xz>, fetched
  2026-10-05 (last modified 2025-07-26).

  Build commit: `c195113dbaf09488f8d5eeb2db91dacd123b74d0`
  ("need sdk change for release mode", 2025-07-26). The build uses
  wasi-sdk 25.0, `--target=wasm32-wasip1`, and statically links `plpgsql`
  and pgvector 0.8.0.

- `pglite-prefix-17.5.tar.xz` is the PostgreSQL 17.x prefix file set from
  [electric-sql/pglite-bindings](https://github.com/electric-sql/pglite-bindings)
  (`17.x/pglite-prefix.tar.xz`), fetched 2026-10-05.

  Repository head at fetch time:
  `0d31326a41f7251548103f73e601699b0ebae2fa` ("wasi instrumentation",
  2025-08-05).

## Contents

~~~text
tmp/pglite/bin/pglite.wasi          PostgreSQL 17.5 (wasm32-wasip1), 23 MB
tmp/pglite/password                 demo superuser password ("password")
tmp/pglite/lib/postgresql/*.so      placeholder library metadata
tmp/pglite/share/postgresql/...     initdb inputs and extension SQL
~~~

## Rebuilding

See `build/README.md` in the repository root. The build pipeline pins
wasi-sdk 25.0 and the pglite-build revision above, adds the pgvector symbol
registration patch, and publishes a new tarball plus updated checksums.

## Licences

PGlite is dual-licensed Apache-2.0 / PostgreSQL License. The postgres-pglite
changes and pgvector are PostgreSQL License. libpglite is MIT. See
`THIRD_PARTY_NOTICES.md`.
