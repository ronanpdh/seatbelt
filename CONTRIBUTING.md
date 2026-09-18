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

New functionality comes with tests, and a bug fix comes with a test that fails without it. Tests are `pytest` under `tests/`, never touch a live model or the network, and use recorded fixtures or ledgers written in `tmp_path`. A change to the shipped scenario corpus updates `tests/fixtures/corpus.sha256`; a change to a schema model reruns `scripts/export_schemas.py`.

## Changes

- Everything lands on `main` by pull request.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) (`feat:`, `fix:`, `docs:`, `chore:` ...).
- Add a line under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.
- A change to the ledger schema needs a schema version bump, a changelog note and, if it changes a design decision, an ADR in [docs/adr/](docs/adr/).

## Security issues

Do not open a public issue. Follow [SECURITY.md](SECURITY.md).

## Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
