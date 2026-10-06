#!/usr/bin/env bash
# Build pglite.wasi with the static extensions statically linked.
#
# Reproduces the upstream electric-sql/pglite-build "portable" pipeline with
# one patch that:
#   * builds static extra extensions in WASI mode
#   * registers the statically linked extension symbols (pgvector and the
#     contrib modules) in the dlopen/dlsym shim
#   * enables PostgreSQL's sigsetjmp/siglongjmp error handling through Wasm
#     exception handling and links the wasi-libc setjmp runtime
#   * clears ActivePortal during trap recovery in libpglite
#   * compiles the core, the runtime and the extensions at -O3
#   * bounds the core compile parallelism
#
# The pipeline uses the newest wasi-sdk release (dev override with
# WASI_SDK_VERSION) and translates the linked module from the legacy to the
# new exception-handling encoding with Binaryen, because LLVM still lowers
# setjmp/longjmp to the legacy instructions while wasmtime implements only the
# new proposal.  See build/README.md.
#
# Requirements: Linux x86_64, ~5 GB free disk, network access. The build uses
# the proot-based Alpine container shipped by pglite-build (no Docker daemon
# needed, although a Dockerfile is also present upstream).
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BUILD_DIR=${PGLITE_BUILD_DIR:-/tmp/opencode/pglite-build}
TOOLS_DIR=${PGLITE_TOOLS_DIR:-/tmp/opencode/pglite-tools}
PGLITE_BUILD_PIN=${PGLITE_BUILD_PIN:-c195113dbaf09488f8d5eeb2db91dacd123b74d0}
PG_VERSION=${PG_VERSION:-17.5}
PG_BRANCH=${PG_BRANCH:-REL_17_5_WASM}
PGVECTOR_VERSION=${PGVECTOR_VERSION:-0.8.0}
WASI_SDK_VERSION=${WASI_SDK_VERSION:-34.0}
WASI_SDK_ARCH=${WASI_SDK_ARCH:-x86_64}
BINARYEN_VERSION=${BINARYEN_VERSION:-version_133}
BINARYEN_ARCH=${BINARYEN_ARCH:-x86_64-linux}

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
echo "  tools dir:   ${TOOLS_DIR}"
echo "  pglite pin:  ${PGLITE_BUILD_PIN}"
echo "  postgres:    ${PG_VERSION} (${PG_BRANCH})"
echo "  pgvector:    ${PGVECTOR_VERSION}"
echo "  wasi-sdk:    ${WASI_SDK_VERSION}"
echo "  binaryen:    ${BINARYEN_VERSION}"
echo "  jobs:        ${PGLITE_JOBS}"

mkdir -p "${TOOLS_DIR}"

download() {
    # download <path> <url>
    local path="$1"
    local url="$2"
    if [ -f "${path}" ]; then
        return 0
    fi
    echo "downloading $(basename "${path}")"
    curl -sSL --retry 3 "${url}" -o "${path}"
}

WASI_SDK_ARCHIVE="wasi-sdk-${WASI_SDK_VERSION}-${WASI_SDK_ARCH}-linux.tar.gz"
WASI_SDK_TARBALL="${TOOLS_DIR}/${WASI_SDK_ARCHIVE}"
WASI_SDK_URL="https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-${WASI_SDK_VERSION%%.*}/${WASI_SDK_ARCHIVE}"
download "${WASI_SDK_TARBALL}" "${WASI_SDK_URL}"
export WASI_SDK_TARBALL
export WASI_SDK_VERSION

# wasi-sdk 26 and newer link the clang driver against libedit, which needs a
# glibc libtinfo that the proot container does not ship.  Provide a compatible
# build (Debian bookworm needs glibc 2.33 at most) for portable.sh and for an
# already extracted SDK tree.
LIBTINFO_DEB="${TOOLS_DIR}/libtinfo6_6.4-4_amd64.deb"
LIBTINFO_SO="${TOOLS_DIR}/libtinfo.so.6"
LIBTINFO_DEB_URL="https://deb.debian.org/debian/pool/main/n/ncurses/libtinfo6_6.4-4_amd64.deb"
download "${LIBTINFO_DEB}" "${LIBTINFO_DEB_URL}"
if [ ! -f "${LIBTINFO_SO}" ]; then
    libtinfo_tmp=$(mktemp -d)
    (cd "${libtinfo_tmp}" && ar x "${LIBTINFO_DEB}" && tar xf data.tar.*)
    cp -f "${libtinfo_tmp}/lib/x86_64-linux-gnu/libtinfo.so.6.4" "${LIBTINFO_SO}"
    rm -rf "${libtinfo_tmp}"
fi
CONTAINER_PATH=${CONTAINER_PATH:-/tmp/fs}
mkdir -p "${CONTAINER_PATH}/tmp"
cp -f "${LIBTINFO_SO}" "${CONTAINER_PATH}/tmp/wasi-libtinfo.so.6"
export WASI_LIBTINFO=/tmp/wasi-libtinfo.so.6

BINARYEN_DIR="${TOOLS_DIR}/binaryen-${BINARYEN_VERSION}"
WASM_OPT="${BINARYEN_DIR}/bin/wasm-opt"
if [ ! -x "${WASM_OPT}" ]; then
    BINARYEN_ARCHIVE="binaryen-${BINARYEN_VERSION}-${BINARYEN_ARCH}.tar.gz"
    download "${TOOLS_DIR}/${BINARYEN_ARCHIVE}" "https://github.com/WebAssembly/binaryen/releases/download/${BINARYEN_VERSION}/${BINARYEN_ARCHIVE}"
    tar xzf "${TOOLS_DIR}/${BINARYEN_ARCHIVE}" -C "${TOOLS_DIR}"
fi
if [ ! -x "${WASM_OPT}" ]; then
    echo "wasm-opt not found at ${WASM_OPT}"
    exit 27
fi

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
rm -rf "${CONTAINER_PATH}/tmp/pglite/pg.wasi.installed" \
       "${CONTAINER_PATH}/tmp/pglite/share" \
       "${CONTAINER_PATH}/tmp/pglite/include" \
       "${CONTAINER_PATH}/tmp/pglite/lib" \
       "${CONTAINER_PATH}/tmp/pglite/bin" \
       "${CONTAINER_PATH}/tmp/pglite"/config.cache.*

# The SDK bundle's hotfix header predates wasi-libc's wasi/wasip1.h split, uses
# the WASI types without including the header that declares them, and stubs
# sigsetjmp/siglongjmp; build/extra/hotfix-patch.h is the fixed copy.
HOTFIX_FIXED="${REPO_ROOT}/build/extra/hotfix-patch.h"
cp -f "${HOTFIX_FIXED}" "${CONTAINER_PATH}/tmp/wasi-hotfix-patch.h"
export WASI_HOTFIX_PATCH=/tmp/wasi-hotfix-patch.h

# A previously used container keeps its wasi-sdk extracted; swap in the
# requested release.  On a fresh container portable.sh installs
# WASI_SDK_TARBALL itself (patched portable/portable.sh).
# Header fixes the portable bundle applied on top of wasi-libc: sun_path in
# struct sockaddr_un and the missing SysV shared-memory headers.  The official
# wasi-sdk overwrites them, so extract them for both the cached and the fresh
# SDK path.
SYSFIX_DIR="${CONTAINER_PATH}/tmp/wasi-sysfix"
rm -rf "${SYSFIX_DIR}"
mkdir -p "${SYSFIX_DIR}"
for sysfix_header in __struct_sockaddr_un.h bits/shm.h sys/shm.h; do
    mkdir -p "${SYSFIX_DIR}/$(dirname "${sysfix_header}")"
    tar xOJf "${BUILD_DIR}/portable/wasi-sdk-25.tar.xz" \
        "tmp/sdk/wasisdk/sysfix/share/wasi-sysroot/include/wasm32-wasi/${sysfix_header}" \
        > "${SYSFIX_DIR}/${sysfix_header}"
done
export WASI_SYSFIX_DIR=/tmp/wasi-sysfix

# A previously used container keeps its wasi-sdk extracted; always reinstall
# the requested release plus the header fixes and tooling it needs.  On a
# fresh container portable.sh installs WASI_SDK_TARBALL itself (patched
# portable/portable.sh).
WASISDK_DIR="${CONTAINER_PATH}/tmp/sdk/wasisdk"
if [ -x "${WASISDK_DIR}/bin/wasi-c" ]; then
    echo "installing wasi-sdk ${WASI_SDK_VERSION} with the portable sysroot fixes"
    rm -rf "${WASISDK_DIR}/upstream"
    mkdir -p "${WASISDK_DIR}/upstream"
    sdk_extract=$(mktemp -d)
    tar xzf "${WASI_SDK_TARBALL}" -C "${sdk_extract}"
    cp -a "${sdk_extract}"/wasi-sdk-${WASI_SDK_VERSION}-*-linux/. "${WASISDK_DIR}/upstream/"
    rm -rf "${sdk_extract}"
    cp -R "${SYSFIX_DIR}/." \
        "${WASISDK_DIR}/upstream/share/wasi-sysroot/include/wasm32-wasip1/"
    cp -f "${LIBTINFO_SO}" "${WASISDK_DIR}/upstream/lib/libtinfo.so.6"
    if [ -d "${WASISDK_DIR}/hotfix" ]; then
        cp -f "${HOTFIX_FIXED}" "${WASISDK_DIR}/hotfix/patch.h"
    fi
fi

cp "${REPO_ROOT}/build/extra/extensions-wasi.sh" "${BUILD_DIR}/extra/"
cp "${REPO_ROOT}/build/extra/static-extensions.list" "${BUILD_DIR}/extra/"
cp -R "${REPO_ROOT}/build/extra/fuzzystrmatch" "${BUILD_DIR}/extra/"
cp "${REPO_ROOT}/build/scripts/gen_extension_symbols.py" "${BUILD_DIR}/wasm-build/"
chmod +x "${BUILD_DIR}/extra/extensions-wasi.sh"

# Extra patches for the postgres-pglite tree; portable.sh applies every
# patches-<branch>/postgresql-*/*.diff after cloning/updating it.
PG_PATCH_DIR="${BUILD_DIR}/patches-${PG_BRANCH}/postgresql-wasi"
mkdir -p "${PG_PATCH_DIR}"
cp "${REPO_ROOT}"/build/extra/pg-patches/*.diff "${PG_PATCH_DIR}/"

# PG_EXTRA is build-<branch>/extra-wasi inside the container; keep the sources
# where the patched build expects them.
PG_EXTRA_DIR="${BUILD_DIR}/build-${PG_BRANCH}"
mkdir -p "${PG_EXTRA_DIR}"
if [ ! -d "${PG_EXTRA_DIR}/extra-wasi/vector/src" ]; then
    echo "fetching pgvector ${PGVECTOR_VERSION}"
    curl -sSL "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz" \
        -o "${BUILD_DIR}/pgvector.tar.gz"
    mkdir -p "${PG_EXTRA_DIR}/extra-wasi"
    tar xzf "${BUILD_DIR}/pgvector.tar.gz" -C "${PG_EXTRA_DIR}/extra-wasi"
    mv "${PG_EXTRA_DIR}/extra-wasi/pgvector-${PGVECTOR_VERSION}" \
       "${PG_EXTRA_DIR}/extra-wasi/vector"
fi

cd "${BUILD_DIR}"
WASI=true CI=true DEBUG=false \
WASI_SDK="${WASI_SDK_VERSION}" \
PG_VERSION="${PG_VERSION}" PG_BRANCH="${PG_BRANCH}" \
    nice -n 10 bash ./ci-alpine-proot.sh

DIST="${BUILD_DIR}/dist-${PG_BRANCH}"

# LLVM still lowers PostgreSQL's setjmp/longjmp to the legacy exception
# handling instructions; wasmtime implements the new proposal only.  Translate
# the linked module and repack the filesystem tarball with it.
if [ ! -x "${WASM_OPT}" ]; then
    echo "wasm-opt not found at ${WASM_OPT}"
    exit 27
fi
"${WASM_OPT}" --translate-to-new-eh --enable-exception-handling \
    "${DIST}/pglite.wasi" -o "${DIST}/pglite.wasi.new"
mv -f "${DIST}/pglite.wasi.new" "${DIST}/pglite.wasi"

if [ -f "${DIST}/pglite-wasi.tar.xz" ]; then
    work_dir=$(mktemp -d)
    tar xJf "${DIST}/pglite-wasi.tar.xz" -C "${work_dir}"
    cp "${DIST}/pglite.wasi" "${work_dir}/tmp/pglite/bin/pglite.wasi"
    (cd "${work_dir}" && tar cJf "${DIST}/pglite-wasi.tar.xz" tmp/pglite)
    rm -rf "${work_dir}"
fi

echo
echo "build finished"
ls -la "${DIST}" | head
echo
echo "pglite.wasi:            ${DIST}/pglite.wasi"
echo "pglite-wasi.tar.xz:     ${DIST}/pglite-wasi.tar.xz (extensions: vector + build/extra/static-extensions.list)"
