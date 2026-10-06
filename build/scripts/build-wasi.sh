#!/usr/bin/env bash
# Build pglite.wasi with pgvector and pg_trgm statically linked.
#
# Reproduces the upstream electric-sql/pglite-build "portable" pipeline with
# one patch that:
#   * builds static extra extensions in WASI mode
#   * registers the statically linked extension symbols (pgvector, pg_trgm)
#     in the dlopen/dlsym shim
#   * clears ActivePortal during trap recovery in libpglite
#   * bounds the core compile parallelism
#
# Requirements: Linux x86_64, ~5 GB free disk, network access. The build uses
# the proot-based Alpine container shipped by pglite-build (no Docker daemon
# needed, although a Dockerfile is also present upstream).
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BUILD_DIR=${PGLITE_BUILD_DIR:-/tmp/opencode/pglite-build}
PGLITE_BUILD_PIN=${PGLITE_BUILD_PIN:-c195113dbaf09488f8d5eeb2db91dacd123b74d0}
PG_VERSION=${PG_VERSION:-17.5}
PG_BRANCH=${PG_BRANCH:-REL_17_5_WASM}
PGVECTOR_VERSION=${PGVECTOR_VERSION:-0.8.0}

# The upstream build defaults to `make -j$(nproc)`, which is dangerous on
# large machines: each cc1 can use hundreds of MB and 128 jobs will exhaust
# memory. Keep the parallelism sane and overridable.
PGLITE_JOBS=${PGLITE_JOBS:-8}
export PGLITE_JOBS
export NCPU="${PGLITE_JOBS}"
export MAKEFLAGS="-j${PGLITE_JOBS}"

echo "pytest-pglite WASM build"
echo "  repo:        ${REPO_ROOT}"
echo "  build dir:   ${BUILD_DIR}"
echo "  pglite pin:  ${PGLITE_BUILD_PIN}"
echo "  postgres:    ${PG_VERSION} (${PG_BRANCH})"
echo "  pgvector:    ${PGVECTOR_VERSION}"
echo "  jobs:        ${PGLITE_JOBS}"

if [ ! -d "${BUILD_DIR}/.git" ]; then
    git clone --branch portable \
        https://github.com/electric-sql/pglite-build "${BUILD_DIR}"
fi
git -C "${BUILD_DIR}" fetch --all --tags
git -C "${BUILD_DIR}" checkout --force "${PGLITE_BUILD_PIN}"
git -C "${BUILD_DIR}" clean -fdx

git -C "${BUILD_DIR}" apply "${REPO_ROOT}/build/patches/0001-static-extensions.patch"

# The proot container keeps the installed postgres prefix in /tmp/fs between
# runs, while the build tree is cleaned above. Remove the stale install marker
# and prefix files or the core build is skipped and the link finds no objects
# (this mirrors the cleanup in upstream CI.yml).
CONTAINER_PATH=${CONTAINER_PATH:-/tmp/fs}
rm -rf "${CONTAINER_PATH}/tmp/pglite/pg.wasi.installed" \
       "${CONTAINER_PATH}/tmp/pglite/share" \
       "${CONTAINER_PATH}/tmp/pglite/include" \
       "${CONTAINER_PATH}/tmp/pglite/lib" \
       "${CONTAINER_PATH}/tmp/pglite/bin"

cp "${REPO_ROOT}/build/extra/extensions-wasi.sh" "${BUILD_DIR}/extra/"
cp "${REPO_ROOT}/build/scripts/gen_extension_symbols.py" "${BUILD_DIR}/wasm-build/"
chmod +x "${BUILD_DIR}/extra/extensions-wasi.sh"

# PG_EXTRA is build-<branch>/extra-wasi inside the container; keep the sources
# where the patched build expects them.
PG_EXTRA_DIR="${BUILD_DIR}/build-${PG_BRANCH}/extra-wasi"
mkdir -p "${PG_EXTRA_DIR}"
if [ ! -d "${PG_EXTRA_DIR}/vector/src" ]; then
    echo "fetching pgvector ${PGVECTOR_VERSION}"
    curl -sSL "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz" \
        -o "${BUILD_DIR}/pgvector.tar.gz"
    tar xzf "${BUILD_DIR}/pgvector.tar.gz" -C "${PG_EXTRA_DIR}"
    mv "${PG_EXTRA_DIR}/pgvector-${PGVECTOR_VERSION}" "${PG_EXTRA_DIR}/vector"
fi

cd "${BUILD_DIR}"
# The 0001 patch makes the WASI release build compile the PostgreSQL core at
# -O2 (upstream leaves the core CFLAGS without an optimization level, so
# clang defaults to -O0 and SQL execution is roughly twice as slow).
WASI=true CI=true DEBUG=false \
PG_VERSION="${PG_VERSION}" PG_BRANCH="${PG_BRANCH}" \
    nice -n 10 bash ./ci-alpine-proot.sh

DIST="${BUILD_DIR}/dist-${PG_BRANCH}"
echo
echo "build finished"
ls -la "${DIST}" | head
echo
echo "pglite.wasi:            ${DIST}/pglite.wasi"
echo "pglite-wasi.tar.xz:     ${DIST}/pglite-wasi.tar.xz (pgvector ${PGVECTOR_VERSION}, pg_trgm)"
