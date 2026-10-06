#!/bin/bash
# Prepare pgvector for static linking into the WASI module.
#
# Runs inside the pglite-build container. Compilation happens later in
# pglite-wasm/build.sh, where the WASI compiler environment is available; this
# script installs the SQL/control files and generates the dlsym registration
# table consumed by wasm-build/sdk_port-wasi/sdk_port-wasi-dlfcn.c.
set -e

. wasm-build/extension.sh

VECTOR=${PG_EXTRA}/vector
if [ ! -d "${VECTOR}/src" ]; then
    echo "pgvector sources not found at ${VECTOR}"
    exit 21
fi

mkdir -p "${PGROOT}/share/postgresql/extension"
cp "${VECTOR}/vector.control" "${PGROOT}/share/postgresql/extension/"
cp "${VECTOR}/sql/vector.sql" \
   "${PGROOT}/share/postgresql/extension/vector--0.8.0.sql"

python3 wasm-build/gen_vector_symbols.py \
    "${PGROOT}/share/postgresql/extension/vector--0.8.0.sql" \
    > "${WORKSPACE}/wasm-build/sdk_port-wasi/vector_symbols.h"

echo "vector: installed extension SQL and generated symbol table"
