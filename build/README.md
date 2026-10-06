# Building the bundled pglite.wasi artifact

`pytest-pglite` ships a prebuilt PostgreSQL 17.5 WebAssembly module with
pgvector and pg_trgm statically linked, under `src/pytest_pglite/_wasm/`. This
directory contains everything needed to reproduce that artifact.

## What gets built

The build starts from
[electric-sql/pglite-build](https://github.com/electric-sql/pglite-build)
(branch `portable`, pinned commit `c195113dbaf09488f8d5eeb2db91dacd123b74d0`)
and applies `patches/0001-static-extensions.patch`, which:

1. `wasm-build/build-ext.sh` - in WASI mode, run `extra/*-wasi.sh` build steps
   instead of skipping all extensions.
2. `extra/extensions-wasi.sh` (added separately, not part of the patch) -
   install `vector.control` / `vector--0.8.0.sql` and the `pg_trgm.control`
   plus `pg_trgm--*.sql` scripts, create the `vector.so` / `pg_trgm.so`
   placeholders that PostgreSQL stats before `dlopen`, and generate the dlsym
   table.
3. `wasm-build/gen_extension_symbols.py` (added separately) - parse the
   extension SQL and emit one table entry per C symbol, honouring
   `AS 'MODULE_PATHNAME', 'symbol'` aliases, for every bundled extension.
4. `wasm-build/sdk_port-wasi/sdk_port-wasi-dlfcn.c` - consult the generated
   table from `dlsym`, call each extension's `_PG_init` from `dlopen`, and fix
   the upstream `dltab` bug (a one-element tentative array with an off-by-one
   that corrupted memory on the second `dlopen`).
5. `pglite-REL_17_4_WASM/build.sh` - compile pgvector's and pg_trgm's C sources
   (with `_PG_init` renamed to `pglite_vector_PG_init` /
   `pglite_pg_trgm_PG_init`, and pg_trgm's `Pg_magic_func` renamed, because
   other objects define those symbols) and add them to the `pglite.wasi`
   link.
6. `pglite-REL_17_4_WASM/interactive_one.c` - clear `ActivePortal` during
   trap recovery. Without `sigsetjmp` the active portal is never unwound and
   `PortalErrorCleanup()` aborts with "cannot drop active portal", wedging the
   session after a simple-query error.
7. `wasm-build/build-pgcore.sh` - honour `PGLITE_JOBS` instead of `nproc`, and
   append `${COPTS}` to the core CFLAGS.
8. `wasm-build.sh` - the WASI release build compiles the core at `-O2`
   (`COPTS`/`LOPTS`); upstream leaves the CFLAGS without an optimization flag,
   so clang defaults to `-O0` and CPU-bound SQL is roughly twice as slow.
9. `wasmfs.txt` - ship the extension SQL, control and placeholder files.

## Running the build

~~~bash
build/scripts/build-wasi.sh
~~~

Requirements: Linux x86_64, network access, `wget`, `lz4`, `xz`, `tar`,
`patch`, about 5 GB of free disk. The build runs in the proot-based Alpine
container shipped by pglite-build; no Docker daemon is needed. Expect 30-90
minutes on a cold container.

Environment overrides:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PGLITE_BUILD_DIR` | `/tmp/opencode/pglite-build` | pglite-build checkout |
| `PGLITE_BUILD_PIN` | `c195113...` | pglite-build commit to build |
| `PG_VERSION` | `17.5` | PostgreSQL version |
| `PG_BRANCH` | `REL_17_5_WASM` | postgres-pglite branch |
| `PGVECTOR_VERSION` | `0.8.0` | pgvector version |
| `PGLITE_JOBS` | `8` | Parallel compiler jobs. Upstream defaults to `nproc`, which can exhaust memory on large build hosts; the patch adds this override |

Outputs:

- `dist-<branch>/pglite.wasi` - the module itself
- `dist-<branch>/pglite-wasi.tar.xz` - the module plus the minimal filesystem
  (share files, password, placeholder binaries)

## Updating the vendored artifact

1. Run the build.
2. Verify it boots: `pytest -m integration tests/test_server.py
   tests/test_vector.py tests/test_pg_trgm.py` after copying the files (or
   point `--pglite-wasm` at the build tree).
3. Copy the tarballs into `src/pytest_pglite/_wasm/`, update `SHA256SUMS` and
   both `PROVENANCE.md` files, and bump the artifact filename/version in
   `src/pytest_pglite/artifacts.py`.

## Known limitations of the upstream WASI build

- No `sigsetjmp`/`longjmp`, so any SQL error aborts the WebAssembly instance.
  The Python host captures the ErrorResponse emitted before the abort, and if
  the session cannot be recovered it restarts the instance over the same data
  directory. The `0001` patch removes the most common recovery wedge; a build
  with `-mllvm -wasm-enable-sjlj` is future work.
- Extensions must be compiled into the module; only `plpgsql`, the snowball
  dictionaries, `vector` and `pg_trgm` are wired today.
  `gen_extension_symbols.py` is the template for adding more.
- `bin/postgres` and `bin/initdb` inside the tarball are empty placeholders
  that satisfy the initdb code paths that probe for executables.
