#!/bin/bash
# Install the statically linked extensions (pgvector, pg_trgm) for the WASI
# module and generate the dlopen/dlsym symbol registration table.
#
# Runs inside the pglite-build container. Compilation happens later in
# pglite-wasm/build.sh, where the WASI compiler environment is available; this
# script installs the SQL/control files, creates the .so placeholders that
# PostgreSQL stats before dlopen, and parses the extension SQL to emit the
# symbol table consumed by wasm-build/sdk_port-wasi/sdk_port-wasi-dlfcn.c.
set -e

. wasm-build/extension.sh

SHARE_EXT="${PGROOT}/share/postgresql/extension"
mkdir -p "${SHARE_EXT}" "${PGROOT}/lib/postgresql"

# pgvector sources are fetched by build/scripts/build-wasi.sh into extra-wasi.
VECTOR=${PG_EXTRA}/vector
[ -d "${VECTOR}/src" ] || VECTOR="${WORKSPACE}/extra/vector"
if [ ! -d "${VECTOR}/src" ]; then
    echo "pgvector sources not found at ${VECTOR}"
    exit 21
fi

touch "${PGROOT}/lib/postgresql/vector.so"
cp "${VECTOR}/vector.control" "${SHARE_EXT}/"
cp "${VECTOR}/sql/vector.sql" "${SHARE_EXT}/vector--0.8.0.sql"

# pg_trgm is a PostgreSQL contrib module: the sources live in the patched
# PostgreSQL tree and are compiled into the link by pglite-wasm/build.sh.
TRGM="${WORKSPACE}/postgresql-${PG_BRANCH}/contrib/pg_trgm"
[ -d "${TRGM}" ] || TRGM="${PGSRC}/contrib/pg_trgm"
if [ ! -d "${TRGM}" ]; then
    echo "pg_trgm sources not found at ${TRGM}"
    exit 22
fi

touch "${PGROOT}/lib/postgresql/pg_trgm.so"
cp "${TRGM}/pg_trgm.control" "${SHARE_EXT}/"
cp "${TRGM}"/*.sql "${SHARE_EXT}/"

python3 wasm-build/gen_extension_symbols.py \
    "${WORKSPACE}/wasm-build/sdk_port-wasi/extension_symbols.h" \
    "${SHARE_EXT}/vector--0.8.0.sql" \
    "${SHARE_EXT}"/pg_trgm--*.sql

echo "static extensions: installed vector and pg_trgm, generated symbol table"
