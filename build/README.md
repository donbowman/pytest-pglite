# Building the bundled pglite.wasi artifact

`pytest-pglite` ships a prebuilt PostgreSQL 17.5 WebAssembly module with
pgvector and a set of contrib extensions statically linked, under
`src/pytest_pglite/_wasm/`. This directory contains everything needed to
reproduce that artifact.

## What gets built

The build starts from
[electric-sql/pglite-build](https://github.com/electric-sql/pglite-build)
(branch `portable`, pinned commit `c195113dbaf09488f8d5eeb2db91dacd123b74d0`)
and applies `patches/0001-static-extensions.patch`, which:

1. `wasm-build/build-ext.sh` - in WASI mode, run `extra/*-wasi.sh` build steps
   instead of skipping all extensions.
2. `extra/extensions-wasi.sh` (added separately, not part of the patch) -
   install every bundled extension's control/SQL files, create the `.so`
   placeholders that PostgreSQL stats before `dlopen`, copy data files such as
   `unaccent.rules`, and generate the dlsym table.
3. `wasm-build/gen_extension_symbols.py` (added separately) - parse the
   extension SQL and emit one table entry per C symbol, honouring
   `AS 'MODULE_PATHNAME', 'symbol'` aliases, for every bundled extension.
4. `wasm-build/sdk_port-wasi/sdk_port-wasi-dlfcn.c` - consult the generated
   table from `dlsym`, call each extension's `_PG_init` from `dlopen`, and fix
   the upstream `dltab` bug (a one-element tentative array with an off-by-one
   that corrupted memory on the second `dlopen`).
5. `pglite-REL_17_4_WASM/build.sh` - compile pgvector (from `extra-wasi/`) and
   every module in `extra/static-extensions.list`, add the objects to the
   `pglite.wasi` link, and link the wasi-libc setjmp runtime.
6. `wasm-build.sh` - compile the core, the runtime and the extensions at
   `-O3` (upstream left the core CFLAGS without an optimization flag, so
   clang defaulted to `-O0`), and enable PostgreSQL's `sigsetjmp`/`longjmp`
   error handling with `-mllvm -wasm-enable-sjlj`, the `sigsetjmp` to
   `setjmp` mapping, and the `PGLITE_ENABLE_SJLJ` marker.
7. `pglite-REL_17_4_WASM/pgl_sjlj.c` - compile the real sigsetjmp error
   recovery path on WASI when `PGLITE_ENABLE_SJLJ` is defined.
8. `pglite-REL_17_4_WASM/pg_main.c` - assign to `stdin` through a cast
   because wasi-libc 26 and newer declare it as `FILE *const`.
9. `wasm-build/build-pgcore.sh` - honour `PGLITE_JOBS` instead of `nproc`,
   append `${COPTS}` to the core CFLAGS, and add `-lsetjmp` to the WASI link
   flags.
10. `wasm-build/sdk_port.h` and
    `patches-REL_17_5_WASM/postgresql-wasi/src-include-port-wasi.h.diff` -
    rename the port's static stubs (`pipe`, `dup`, `semctl`, ...) and add
    `#define`s because clang 23 rejects a static definition after wasi-libc's
    now-visible declaration.
11. `wasmfs.txt` - ship the whole `lib/postgresql` and
    `share/postgresql/extension` directories.
12. `portable/portable.sh` - install the wasi-sdk release requested by the
    orchestrator, plus the libtinfo build and the fixed hotfix header it
    needs.
13. `pglite-REL_17_4_WASM/interactive_one.c` - keep clearing `ActivePortal`
    during trap recovery, so the remaining emergency restart path stays
    useful.

The orchestrator (`scripts/build-wasi.sh`) adds three things around the
upstream build:

- **wasi-sdk 34 (LLVM 23)**: downloads the official release into
  `PGLITE_TOOLS_DIR` and installs it into the container, together with a
  glibc `libtinfo.so.6` (clang 23 links libedit) and
  `extra/hotfix-patch.h` (the SDK bundle's hotfix header, fixed for the new
  wasi-libc headers and with the `sigsetjmp`/`siglongjmp` stubs disabled when
  `PGLITE_ENABLE_SJLJ` is set).
- **Binaryen**: downloads a pinned `wasm-opt` and translates the linked
  module from the legacy exception-handling encoding that LLVM still emits for
  setjmp/longjmp to the new `try_table` encoding, which is the only one
  wasmtime implements.
- **Repacking**: replaces the module inside `pglite-wasi.tar.xz` with the
  translated one.

## Extensions

The bundled extension set is `extra/static-extensions.list`:
btree_gin, btree_gist, bloom, citext, cube, dict_int, earthdistance,
fuzzystrmatch, hstore, intarray, isn, ltree, pg_trgm, seg, tablefunc,
tsm_system_rows, tsm_system_time and unaccent, plus pgvector from its own
repository. Modules that need external libraries are not bundled:
`uuid-ossp` needs OSSP UUID and `pgcrypto` is only built by PostgreSQL when
OpenSSL is enabled, which wasi-sdk does not provide.

`extensions-wasi.sh` also runs the module-specific generators before
compilation: Perl for `fuzzystrmatch`'s Daitch-Mokotoff tables and flex/bison
for the `seg` and `cube` scanner/parser sources.

## Running the build

~~~bash
build/scripts/build-wasi.sh
~~~

Requirements: Linux x86_64, network access, `curl`, `xz`, `tar`, `patch`,
`ar` and about 5 GB of free disk. The build runs in the proot-based Alpine
container shipped by pglite-build; no Docker daemon is needed. Expect 30-90
minutes on a cold container.

Environment overrides:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PGLITE_BUILD_DIR` | `/tmp/opencode/pglite-build` | pglite-build checkout |
| `PGLITE_TOOLS_DIR` | `/tmp/opencode/pglite-tools` | downloaded toolchain cache |
| `PGLITE_BUILD_PIN` | `c195113...` | pglite-build commit to build |
| `WASI_SDK_VERSION` | `34.0` | wasi-sdk release |
| `BINARYEN_VERSION` | `version_133` | Binaryen release |
| `PG_VERSION` | `17.5` | PostgreSQL version |
| `PG_BRANCH` | `REL_17_5_WASM` | postgres-pglite branch |
| `PGVECTOR_VERSION` | `0.8.0` | pgvector version |
| `PGLITE_JOBS` | `8` | Parallel compiler jobs. Upstream defaults to `nproc`, which can exhaust memory on large build hosts; the patch adds this override |

Outputs:

- `dist-<branch>/pglite.wasi` - the translated module itself
- `dist-<branch>/pglite-wasi.tar.xz` - the module plus the minimal filesystem
  (share files, password, placeholder binaries)

## Updating the vendored artifact

1. Run the build.
2. Verify it boots and that errors recover without a trap:
   `pytest -m integration tests/test_server.py tests/test_vector.py
   tests/test_pg_trgm.py tests/test_extensions.py tests/test_rewrite.py`
   after copying the files, or point `--pglite-wasm` at the build tree.
3. Copy the tarball into `src/pytest_pglite/_wasm/`, update `SHA256SUMS` and
   `PROVENANCE.md`, and bump the artifact filename in
   `src/pytest_pglite/artifacts.py`.

## Known limitations of the upstream WASI build

- `COPY FROM STDIN` cannot stream through libpglite's synchronous transport
  and is rejected with SQLSTATE 0A000; `COPY TO STDOUT` works.
- `LISTEN`/`NOTIFY` delivery and query cancellation are not implemented by the
  wire proxy.
- Extensions must be compiled into the module; add them to
  `extra/static-extensions.list` (and the module's `.so` placeholder/metadata
  handling if the module has data files) and rebuild.
- `bin/postgres` and `bin/initdb` inside the tarball are empty placeholders
  that satisfy the initdb code paths that probe for executables.
