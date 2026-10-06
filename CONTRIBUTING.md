# Contributing

## Development setup

Python 3.14 and [Poetry](https://python-poetry.org/) are required.

```bash
poetry install --all-extras
poetry run pytest              # fast suite
poetry run pytest -n 4         # xdist
poetry run pytest examples     # run one example directory at a time
poetry run black src tests examples
poetry run flake8 src tests examples
poetry run mypy
```

Integration tests boot a real PGlite WASM instance. The first run compiles
the module (about 1 s) and creates a template database; later runs are fast.
The compiled module and template live in the platform cache directory:

```bash
poetry run python -c "from platformdirs import user_cache_dir; print(user_cache_dir('pytest-pglite'))"
```

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/pytest_pglite/engine.py` | Wasmtime host for `pglite.wasi` |
| `src/pytest_pglite/wire/` | PostgreSQL protocol proxy (codec, state, server) |
| `src/pytest_pglite/server.py` | Server lifecycle, work directories, DSNs |
| `src/pytest_pglite/plugin.py` | pytest fixtures and command-line options |
| `src/pytest_pglite/_wasm/` | Vendored artifact, checksums and provenance |
| `build/` | Reproducible WASM build pipeline (see `build/README.md`) |
| `tests/` | Unit, integration, conformance and psql smoke tests |
| `examples/` | Runnable integrations for common Python stacks |

## Testing expectations

- Every bug fix gets a regression test; protocol changes get a conformance
  test in `tests/test_conformance.py`.
- Tests run on Linux and macOS in CI, with and without xdist.
- Avoid asserting exact timings; `tests/test_performance.py` uses generous
  bounds to catch catastrophic regressions only.

## Rebuilding the WASM artifact

See `build/README.md`. The pipeline needs Linux x86_64, network access and
about 5 GB of disk; it uses the upstream proot-based toolchain and honours
`PGLITE_JOBS` (default 8) to bound compiler parallelism.

After a successful build:

1. Run `pytest -m integration` against the new module (point the tests at it
   with `--pglite-wasm /path/to/build`).
2. Update `src/pytest_pglite/_wasm/` artifacts, `SHA256SUMS` and both
   `PROVENANCE.md` files.
3. Bump the version if the PostgreSQL or extension set changed.

## Releasing

Releases are published from GitHub Actions with PyPI trusted publishing; no
tokens are stored.

1. Bump the version: `poetry version 0.1.1` and commit.
2. Tag and push: `git tag v0.1.1 && git push origin v0.1.1`.
3. The `Publish` workflow builds the wheel and sdist and uploads them to PyPI
   through the `pypi` environment.
4. To exercise the pipeline first, run the workflow manually; it publishes to
   TestPyPI by default.

PyPI-side configuration (one time): add pending trusted publishers for the
project with owner `donbowman`, repository `pytest-pglite`, workflow
`publish.yml`, and environment `pypi` (and `testpypi` for the test target).
