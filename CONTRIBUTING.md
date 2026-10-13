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

Releases are automatic. [Python Semantic Release][psr] derives the version from
the conventional commit messages on `main`; there is no manual version bump and
no manual tag. PyPI uses trusted publishing, so no PyPI credentials are stored.

1. Land your change on `main` through a pull request whose **title** is a
   [Conventional Commit][cc] (`feat:` for a minor bump, `fix:` or `perf:` for a
   patch, `!` or a `BREAKING CHANGE:` footer for a major one). Pull requests are
   squash-merged, so the title is the commit message that drives the version.
2. Merging to `main` starts the `Publish` workflow. Its `release` job parses the
   commits since the last tag, stamps the new version into `pyproject.toml`,
   updates `CHANGELOG.md`, commits, tags `vX.Y.Z` and creates a GitHub release.
3. If a release was made, the `build` job builds the sdist and wheel from that
   tag and the `publish-pypi` job uploads them through the `pypi` environment.

To rehearse the pipeline, run the `Publish` workflow manually; it publishes to
TestPyPI by default.

The configuration lives under `[tool.semantic_release]` in `pyproject.toml`. To
preview the next version locally:

```bash
pipx install python-semantic-release
semantic-release -vv --noop version
```

PyPI-side configuration (one time): add trusted publishers for the project with
owner `donbowman`, repository `pytest-pglite`, workflow `publish.yml`, and
environment `pypi` (and `testpypi` for the test target).

Repository-side configuration (one time):

- **Release app.** Create a GitHub App with the repository permission
  `Contents: Read and write` and nothing else, and install it on this repository
  only. Then store its Client ID as the repository variable
  `RELEASE_APP_CLIENT_ID` and its private key as the repository secret
  `RELEASE_APP_PRIVATE_KEY`. The `release` job exchanges them for a one hour
  installation token scoped to this repository, so the workflow's own
  `GITHUB_TOKEN` stays read only and no personal credential is stored.
- **Branch protection on `main`.** The `release` job pushes the
  `chore(release): X` commit and the `vX.Y.Z` tag straight to `main`, past the
  rule that requires a pull request. A GitHub App can only do that from a
  ruleset's bypass list: legacy branch protection rules have no bypass list, so
  they cannot admit an app, and `GITHUB_TOKEN` is never eligible. `main` is
  therefore covered by a ruleset (pull request required, the CI checks required,
  linear history, no force pushes or deletions) whose bypass list contains the
  release app set to "Always allow". Create the ruleset first, then remove the
  legacy branch protection rule: the two layer, so there is no window without
  protection.

[psr]: https://python-semantic-release.readthedocs.io/
[cc]: https://www.conventionalcommits.org/
