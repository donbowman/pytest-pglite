# Third-party notices

`pytest-pglite` embeds and redistributes a WebAssembly build of PostgreSQL via
PGlite. The following components are included or depended upon.

## Redistributed artifacts

### PGlite (`src/pytest_pglite/_wasm/pglite-17.5.20250726.tar.xz`)

The bundled `pglite.wasi` module and its prefix files are built from the
[electric-sql/pglite-build](https://github.com/electric-sql/pglite-build)
project (`portable` branch), which patches and builds the
[electric-sql/postgres-pglite](https://github.com/electric-sql/postgres-pglite)
PostgreSQL fork, itself derived from PostgreSQL.

- PGlite client library: Apache License 2.0 OR the PostgreSQL License.
- postgres-pglite changes: PostgreSQL License.
- libpglite (C bridge): MIT License, Copyright (c) 2024 ElectricSQL.

The PostgreSQL License text is reproduced at the end of this file.

## Python dependencies

- [wasmtime](https://pypi.org/project/wasmtime/) - Apache License 2.0 WITH
  LLVM-exception.
- [pytest](https://pypi.org/project/pytest/) - MIT License.
- [pydantic](https://pypi.org/project/pydantic/) - MIT License.
- [pydantic-settings](https://pypi.org/project/pydantic-settings/) - MIT License.
- [platformdirs](https://pypi.org/project/platformdirs/) - MIT License.

## Build-time dependencies (not redistributed in the Python wheel)

- [wasi-sdk](https://github.com/WebAssembly/wasi-sdk) - Apache License 2.0
  WITH LLVM-exception (toolchain, used to produce the bundled module).
- [pgvector](https://github.com/pgvector/pgvector) - PostgreSQL License
  (compiled into the bundled module).
- [PostgreSQL](https://www.postgresql.org/) - PostgreSQL License.

## PostgreSQL License

PostgreSQL Database Management System
(formerly known as Postgres, then as Postgres95)

Portions Copyright (c) 1996-2024, PostgreSQL Global Development Group

Portions Copyright (c) 1994, The Regents of the University of California

Permission to use, copy, modify, and distribute this software and its
documentation for any purpose, without fee, and without a written agreement
is hereby granted, provided that the above copyright notice and this
paragraph and the following two paragraphs appear in all copies.

IN NO EVENT SHALL THE UNIVERSITY OF CALIFORNIA BE LIABLE TO ANY PARTY FOR
DIRECT, INDIRECT, SPECIAL, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, INCLUDING
LOST PROFITS, ARISING OUT OF THE USE OF THIS SOFTWARE AND ITS DOCUMENTATION,
EVEN IF THE UNIVERSITY OF CALIFORNIA HAS BEEN ADVISED OF THE POSSIBILITY OF
SUCH DAMAGE.

THE UNIVERSITY OF CALIFORNIA SPECIFICALLY DISCLAIMS ANY WARRANTIES,
INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS FOR A PARTICULAR PURPOSE. THE SOFTWARE PROVIDED HEREUNDER IS
ON AN "AS IS" BASIS, AND THE UNIVERSITY OF CALIFORNIA HAS NO OBLIGATIONS TO
PROVIDE MAINTENANCE, SUPPORT, UPDATES, ENHANCEMENTS, OR MODIFICATIONS.
