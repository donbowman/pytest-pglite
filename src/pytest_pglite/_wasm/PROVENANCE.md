# Vendored WASM artifact provenance

`pytest-pglite` redistributes a prebuilt PostgreSQL 17.5 WebAssembly module so
that users never need a WebAssembly toolchain. This file records exactly what
is bundled and how to rebuild it.

## Bundled file

| File | Size | SHA-256 |
| --- | --- | --- |
| `pglite-17.5.20261006c.tar.xz` | 2568224 | `e04abc5c3153fe8ca0411756d59863c241d5463b848ba3dc2434aa39b7a63408` |

The hash is also recorded in `SHA256SUMS` next to the artifact and is verified
on every first use.

## How it was built

The module is produced by this repository's build pipeline
(`build/scripts/build-wasi.sh`), which is a fork of
[electric-sql/pglite-build](https://github.com/electric-sql/pglite-build)
`portable` branch at commit
`c195113dbaf09488f8d5eeb2db91dacd123b74d0`, plus
`build/patches/0001-static-extensions.patch` and the helper files in
`build/extra/`:

- builds with **wasi-sdk 34 (LLVM 23)**, installing the glibc `libtinfo` that
  clang 23 needs and the portable bundle's wasi-libc header fixes
  (`sun_path` in `struct sockaddr_un`, SysV shared-memory headers); the fixed
  hotfix header is `build/extra/hotfix-patch.h`
- enables PostgreSQL's `sigsetjmp`/`longjmp` error handling on WASI
  (`-mllvm -wasm-enable-sjlj`, `-lsetjmp`, the `sigsetjmp`/`siglongjmp`
  mapping behind `PGLITE_ENABLE_SJLJ`, and the real re-throw path in
  `elog.c`), so an SQL error aborts the transaction instead of the instance
- translates the linked module from LLVM's legacy `try`/`catch` exception
  instructions to the new `try_table` encoding with Binaryen (`version_133`),
  because wasmtime implements only the new proposal
- compiles the core, the runtime and the extensions at `-O3`
- statically links pgvector 0.8.0 and 18 contrib extensions (see
  `build/extra/static-extensions.list`) and registers their SQL-visible
  symbols in the WASI dlopen/dlsym shim
- fixes the PostgreSQL WASI port for clang 23: renamed static stubs that
  clash with wasi-libc declarations, a const `stdin` assignment, and
  `-nostdlib` plus the WASI crt object for the relocatable core library
- keeps the `ActivePortal` cleanup and bounds compiler parallelism with
  `PGLITE_JOBS`

Build: 2026-10-06, Linux x86_64, wasi-sdk 34.0, PostgreSQL 17.5
(`REL_17_5_WASM`), pgvector 0.8.0, Binaryen version_133.

## Contents

~~~text
tmp/pglite/bin/pglite.wasi          PostgreSQL 17.5 + pgvector + contrib extensions, about 10 MB
tmp/pglite/password                 superuser password ("password")
tmp/pglite/lib/postgresql/*.so      library placeholders (plpgsql, snowball, vector, contrib)
tmp/pglite/share/postgresql/...     initdb inputs, extension SQL and tsearch data
~~~

## Rebuilding

See `build/README.md` in the repository root. After a successful build:

1. Boot the new module and run the test suite against it (point the tests at
   it with `--pglite-wasm /path/to/tmp/pglite`).
2. Copy the tarball here under a dated filename, update `SHA256SUMS` and this
   file, and bump `ARTIFACT_FILENAME` in `src/pytest_pglite/artifacts.py`.

## Licences

PGlite is dual-licensed Apache-2.0 / PostgreSQL License. The postgres-pglite
changes, pgvector and the contrib extensions are PostgreSQL License. libpglite
is MIT. See `THIRD_PARTY_NOTICES.md`.
