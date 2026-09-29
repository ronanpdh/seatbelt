# Contributing

Thanks for helping. The rules the code is held to live in [STANDARDS.md](STANDARDS.md); this page covers the workflow.

## Setup

```sh
uv sync                     # installs the package, dev tools and the anthropic SDK
uv run pre-commit install
```

## Before you open a PR

```sh
uv run pre-commit run --all-files   # whitespace, ruff, pyright
uv run pytest
```

CI runs the same checks on Python 3.12 and 3.13.

If a hook reports that it modified files, run `git add -u` and commit again. Committing while some changes are still unstaged makes pre-commit roll back its own fixes.

## Bugs and ideas

Open a [GitHub issue](https://github.com/ronanpdh/seatbelt/issues). Include the harness version (`seatbelt version`), the command or code you ran, and the ledger or pack if you can share it after redaction. Issues are the public, searchable record; questions and design discussion go there too.

## Tests

New functionality comes with tests, and a bug fix comes with a test that fails without it. Tests are `pytest` under `tests/`, never touch a live model or the network, and use recorded fixtures or ledgers written in `tmp_path`. Code that parses untrusted bytes (ledgers, provider streams) is also fuzzed: `uv run --group fuzz python fuzz/fuzz_parsers.py -max_total_time=60 fuzz/corpus` (Linux, CPython 3.12-3.14; CI runs it on a copy of the seeds). A change to the shipped scenario corpus updates `tests/fixtures/corpus.sha256`; a change to a schema model reruns `scripts/export_schemas.py`.

## Changes

- Everything lands on `main` by pull request.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) (`feat:`, `fix:`, `docs:`, `chore:` ...).
- Add a line under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.
- Dependency floors in `pyproject.toml` move by hand, only when code needs a newer release or the `lowest` workflow's audit reports an advisory against a floor. Dependabot updates `uv.lock` only.
- A change to the ledger schema needs a schema version bump, a changelog note and, if it changes a design decision, an ADR in [docs/adr/](docs/adr/).

## Releasing

The maintainer tags; `.github/workflows/release.yml` does the rest.

1. By pull request, set the version in `pyproject.toml` (`uv version <x.y.z>`) and move `[Unreleased]` in CHANGELOG.md to `## [x.y.z] - <date>`. Check the docs for versions this release makes stale: `git grep -n '<previous x.y.z>' -- README.md SECURITY.md docs ':!docs/plans' ':!docs/adr'` should find only history. Copy-paste commands use `<version>` rather than a number (the image in docs/deploy/gateway.md, for one). Merge.
2. Tag the merge commit: `git tag vx.y.z <sha> && git push origin vx.y.z`. The workflow refuses a tag that does not match the version, or whose commit is not on `main`.
3. The `build` job builds the wheel and sdist without installing the dev dependencies, attests them and writes the SBOM. The `test` job runs the tests separately, with a read-only token. When both pass, the `release` job creates the GitHub release, and the `pypi` job publishes the wheel and sdist to PyPI as `seatbelt-ai` after a required reviewer approves it in the Actions tab. The `image` job pushes the gateway image to GHCR (`:<version>` and `:latest`) only after that.
4. uv is pinned in three places: `version:` in the workflows (ci.yml, lowest.yml and release.yml, which also has the release binary's checksum), and the `uv` stage of `docker/Dockerfile.gateway`, by tag and digest. That stage builds the gateway's wheel with the build backend inside uv, and its requirements from `uv.lock`, offline. Dependabot does not update these; bump them together, inside pyproject's `[build-system]` range, or the image build fails.

**Once, before the first PyPI release:**
- On GitHub, Settings → Environments → New environment `pypi`, with yourself as a required reviewer.
- On PyPI, Account settings → Publishing → add a pending GitHub publisher: project `seatbelt-ai`, owner `ronanpdh`, repository `seatbelt`, workflow `release.yml`, environment `pypi`. A pending publisher does not reserve the name, so add it just before tagging.
- On GitHub, Settings → Rules → Rulesets → New ruleset → New tag ruleset: target `v*`, turn on "Restrict creations", "Restrict updates" and "Restrict deletions", and put only the maintainer in the bypass list, so no one else can start a release.

Design and sources: [docs/plans/2026-09-29-pypi-design.md](docs/plans/2026-09-29-pypi-design.md).

## Security issues

Do not open a public issue. Follow [SECURITY.md](SECURITY.md).

## Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
