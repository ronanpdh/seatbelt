# Publishing to PyPI (0.5.0): design

**Goal:** `uv tool install seatbelt-ai`, then `seatbelt run claude`. Each release goes to PyPI from the existing release workflow, with no stored password or token, and with PyPI's own attestations beside the GitHub ones.

**Status:** a design for review.
- Facts come from the sources in the source map, read 2026-09-29.
- Decisions that are ours are marked "(ours)".
- The maintainer has already chosen the name and the module; see "Decided".

## The name

The PyPI name `seatbelt` is taken by an unrelated project [P0]:
- version 0.1.4, "Responsible AI auditing for LLMs and SLMs", uploaded 2026-04-13;
- its wheel installs a top-level `seatbelt` package and a `seatbelt = seatbelt.cli:main` console script.

We therefore publish under another name, and keep what users type:

| | Before | After |
|---|---|---|
| PyPI distribution | none (installed from git) | **`seatbelt-ai`** |
| Python module | `seatbelt` | `seatbelt` (unchanged) |
| Command | `seatbelt` | `seatbelt` (unchanged) |

**The clash that remains.** The other project also installs a `seatbelt` module and a `seatbelt` command [P0], so the two cannot be installed into the same environment. `uv tool install` puts each tool in its own virtual environment [U1], so the recommended install avoids the clash. The docs will say to install with `uv tool install` or into a dedicated virtual environment, and that the other `seatbelt` must not share it (ours).

## Install commands, after

```sh
uv tool install seatbelt-ai        # the `seatbelt` command, in its own environment [U1]
uvx --from seatbelt-ai seatbelt run claude   # once, without installing [U2]
pip install seatbelt-ai            # into a venv of its own
```

`uvx seatbelt` would fetch the other project, since uvx takes the package name from the command [U2]. So the docs always use `--from seatbelt-ai`.

## Code changes the name needs (ours)

- **Version lookup.** `seatbelt/__init__.py` reads the version with `version("seatbelt")`, and so does the sandbox's version probe [repo: `src/seatbelt/__init__.py`, `src/seatbelt/scenarios/sandbox.py`]. Both look the version up by distribution name, which becomes `seatbelt-ai`. Both change to `seatbelt-ai`. A test will assert that `seatbelt.__version__` equals the version in `pyproject.toml`.
- **`pyproject.toml`:**
  - `name = "seatbelt-ai"`;
  - `project.urls` (homepage, source, changelog, issues);
  - classifiers and keywords;
  - `uv.lock` is regenerated with `uv lock`.
- **Messages and docs that name the install:**
  - the CLI's hint `pip install 'seatbelt[gateway]'` [repo: `src/seatbelt/cli.py`];
  - the README quick start, which uses `uv tool install git+…`;
  - `docs/local-recording.md`.
- **Release files:**
  - the wheel and sdist will be named `seatbelt_ai-<version>…`;
  - the provenance asset, today `seatbelt-<version>.intoto.jsonl` [repo: `.github/workflows/release.yml`], follows as `seatbelt_ai-<version>.intoto.jsonl`.
- **The gateway image** installs whatever wheel is in `dist/` by glob [repo: `docker/Dockerfile.gateway`], so it needs no change.
- **The README on PyPI.** Its cover image is a relative path (`docs/assets/seatbelt-cover.png`), and links go to `docs/…`. They become absolute GitHub URLs, so they work away from GitHub.

## The workflow (ours, following [P1]–[P4])

A new job, `pypi`, in `release.yml`, after `release`:

```yaml
  pypi:
    needs: release           # tests passed, the GitHub release exists
    runs-on: ubuntu-latest
    environment: pypi        # optional, "strongly encouraged" [P2]
    permissions:
      id-token: write        # mandatory for Trusted Publishing [P2]
    steps:
      - uses: actions/download-artifact@<sha>   # the wheel and sdist the release job built
        with: { name: dist, path: dist }
      - uses: pypa/gh-action-pypi-publish@<sha> # release/v1, pinned by SHA like every action here
```

- **Build once, publish separately.** The `release` job already builds and tests. It uploads only the wheel and sdist as an artifact, and `pypi` downloads them. The action's README says to keep building out of the publishing job: scripts injected into the build then cannot use the publishing permission. It also names `upload-artifact` / `download-artifact` as the way to pass the files [P3].
- **Only the two distributions.** The SBOM and the `.intoto.jsonl` stay GitHub release assets and never reach PyPI.
- **`id-token: write` in this job only.** The job level is the one the docs recommend [P2][P4]. No token or password is stored anywhere [P2].
- **Attestations.** The action generates and uploads a signed attestation for each file by default, when publishing through a Trusted Publisher [P3][P5]. The GitHub build provenance and SBOM attestations stay as they are.
- **After the GitHub release.** The release is created first, then PyPI. If the upload fails, the job is re-run. The GitHub release already stands, and the image job is independent of it.
- **Not in a reusable workflow.** Trusted Publishing cannot be used from one [P3]; `release.yml` is a top-level workflow.

## One-time setup on PyPI (the maintainer's)

A new project is created by a **pending publisher**. A pending publisher creates the project on first use, with no manual first upload [P1]. Under PyPI account settings → Publishing, add a GitHub publisher:
- project name `seatbelt-ai`;
- owner `ronanpdh`;
- repository `seatbelt`;
- workflow `release.yml`;
- environment `pypi`.

**Timing.** A pending publisher "does not create a project or reserve a project's name until it is actually used to publish". If someone else registers the name first, the pending publisher is invalidated [P1]. So it should be added just before the first release that publishes. The names are free as of 2026-09-29 [P0].

**On GitHub,** create the `pypi` environment (Settings → Environments). The security model recommends a dedicated environment, and required reviewers are one of the protections it allows [P4]. With a required reviewer, each PyPI upload waits for an approval in the Actions tab (Open, 2).

## Testing (ours)

- **A test** that `seatbelt.__version__` matches `pyproject.toml`, so the rename cannot break the version silently.
- **Locally, before the release:**
  - `uv build`;
  - `unzip -l` the wheel, to check it holds only `seatbelt/`;
  - install it into a fresh venv, then run `seatbelt version` and `seatbelt demo`.
- **The first publish is the test of the workflow.** The action's README says Trusted Publishing cannot be tested in CI [P3]. TestPyPI is supported with `repository-url` and its own publisher [P2][P3]. A dry run there is Open, 1.
- **After the release:** `uv tool install seatbelt-ai` on a clean machine, `seatbelt version`, and the attestations shown on the PyPI file page.

## Decided (2026-09-29)

1. **Distribution name: `seatbelt-ai`.**
2. **Module and command: stay `seatbelt`.** The docs recommend an isolated install.

## Open

1. **Dry run on TestPyPI first?** It needs a second pending publisher on test.pypi.org, and a tag or a manual trigger that publishes there. Recommendation: skip it. The first real release (0.5.0) is the test, and a failed upload can be re-run.
2. **A required reviewer on the `pypi` environment?** It adds a manual approval to each release. Recommendation: yes, as the security model suggests [P4]. You are already the one who tags.
3. **Existing installs from git.** Those installs are a uv tool named `seatbelt`, which owns the `seatbelt` executable. uv does not overwrite executables it did not install, and has `--force` for that [U1]. How an install from another package that uv itself installed behaves has not been checked. Recommendation: the upgrade note says to `uv tool uninstall seatbelt`, then `uv tool install seatbelt-ai`.

## Source map

| Ref | Source | Used for |
|---|---|---|
| P0 | pypi.org/pypi/seatbelt/json and its 0.1.4 wheel, read 2026-09-29; pypi.org/pypi/{seatbelt-ai, seatbelt-agents, seatbelt-ledger, agent-seatbelt}/json returned 404 | the name is taken; the other project's module and command; the alternatives are free |
| P1 | docs.pypi.org/trusted-publishers/creating-a-project-through-oidc | pending publishers create the project on first use; they do not reserve the name, and are invalidated if it is taken first |
| P2 | docs.pypi.org/trusted-publishers/using-a-publisher | the job layout; `id-token: write` mandatory, at job level; environment optional but strongly encouraged; no username, password or token; TestPyPI with `repository-url` |
| P3 | github.com/pypa/gh-action-pypi-publish, README (unstable/v1) | keep building out of the publishing job; pass files with upload/download-artifact; attestations on by default with Trusted Publishing; not from reusable workflows; Trusted Publishing cannot be tested in CI |
| P4 | docs.pypi.org/trusted-publishers/security-model | job-level permissions; a dedicated environment and its protections, e.g. required reviewers |
| P5 | docs.pypi.org/attestations/producing-attestations | with pypa/gh-action-pypi-publish, attestations are generated and uploaded by default |
| U1 | docs.astral.sh/uv/concepts/tools | `uv tool install` creates a virtual environment per tool; uv does not overwrite executables it did not install; `--force` |
| U2 | docs.astral.sh/uv/guides/tools | uvx infers the package from the command; `--from` runs a command from another package |
| repo | at `b2236a7`: `pyproject.toml`, `src/seatbelt/__init__.py`, `src/seatbelt/scenarios/sandbox.py`, `src/seatbelt/cli.py`, `.github/workflows/release.yml`, `docker/Dockerfile.gateway`, `README.md` | what names the distribution today |
