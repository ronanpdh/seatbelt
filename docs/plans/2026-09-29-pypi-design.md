# Publishing to PyPI (0.5.0): design

**Goal:** `uv tool install seatbelt-ai`, then `seatbelt run claude`. Each release goes to PyPI from the existing release workflow, with no stored password or token, and with PyPI's own attestations beside the GitHub ones.

**Status:** decided 2026-09-29 (see "Decided") and built for 0.5.0. Checked claim by claim by a second reader (42 claims; the 8 it found wrong or incomplete are fixed here).
- Facts come from the sources in the source map, read 2026-09-29.
- Decisions that are ours are marked "(ours)".
- The maintainer has already chosen the name and the module; see "Decided".

## The name

The PyPI name `seatbelt` is taken by an unrelated project [P0]:
- version 0.1.4, "Responsible AI auditing for LLMs and SLMs — deception, fairness, sociotechnical risk, regulatory compliance", uploaded 2026-04-13;
- its wheel installs a top-level `seatbelt` package and a `seatbelt = seatbelt.cli:main` console script.

We therefore publish under another name, and keep what users type:

| | Before | After |
|---|---|---|
| PyPI distribution | none (installed from git) | **`seatbelt-ai`** |
| Python module | `seatbelt` | `seatbelt` (unchanged) |
| Command | `seatbelt` | `seatbelt` (unchanged) |

**The clashes that remain.** The other project also installs a `seatbelt` module and a `seatbelt` command [P0].
- **The module.** The two cannot share one environment. `uv tool install` gives each tool its own virtual environment [U1], so an isolated install avoids this clash.
- **The command.** It is not avoided. uv links every tool's executables into one shared directory [U1], so only one `seatbelt` command can be on `PATH`. With the other project already installed as a uv tool, installing `seatbelt-ai` fails with "Executable already exists … use `--force`". That was checked with uv 0.8.17, using two uv tools that export the same command.

The docs will say to install with `uv tool install` or into a dedicated virtual environment, and that only one `seatbelt` command can be installed at a time (ours).

## Install commands, after

```sh
uv tool install seatbelt-ai        # the `seatbelt` command, in its own environment [U1]
uvx --from seatbelt-ai seatbelt run claude   # once, without installing [U2]
pip install seatbelt-ai            # into a venv of its own
```

`uvx seatbelt` would fetch the other project: "the required package is inferred from the command name" [U1][U2]. So the docs always use `--from seatbelt-ai`.

## Code changes the name needs (ours)

- **Version lookup.** `seatbelt/__init__.py` reads the version with `version("seatbelt")`, and so does the sandbox's version probe [repo: `src/seatbelt/__init__.py`, `src/seatbelt/scenarios/sandbox.py`]. Both look the version up by distribution name, which becomes `seatbelt-ai`. Both change to `seatbelt-ai`. A test will assert that `seatbelt.__version__` equals the version in `pyproject.toml`.
- **`pyproject.toml`:**
  - `name = "seatbelt-ai"`;
  - `[tool.uv.build-backend]` with `module-name = "seatbelt"`. The build backend, `uv_build`, otherwise looks for a module named after the distribution. With the name alone changed, `uv build` fails: "Expected a Python module at: src/seatbelt_ai/__init__.py". That breaks CI, `uv sync` and the gateway image's own build. With `module-name` set, the build gives `seatbelt_ai-<version>.tar.gz` and a wheel holding `seatbelt/` [checked on a copy of `b2236a7`];
  - the comment "kept so `seatbelt[gateway]` still installs" becomes `seatbelt-ai[gateway]`;
  - `project.urls` (homepage, source, changelog, issues);
  - classifiers and keywords;
  - `uv.lock` is regenerated with `uv lock`.
- **Messages and docs that name the install:**
  - The CLI's hint `pip install 'seatbelt[gateway]'` [repo: `src/seatbelt/cli.py`] is wrong even today: from PyPI it installs the other project.
  - The README quick start, which uses `uv tool install git+…`.
  - `docs/overview.md`, whose Install section uses git and `uv sync`.
- **Release files:**
  - the wheel and sdist will be named `seatbelt_ai-<version>…`;
  - the provenance asset, today `seatbelt-<version>.intoto.jsonl` [repo: `.github/workflows/release.yml`], follows as `seatbelt_ai-<version>.intoto.jsonl`. `CHANGELOG.md` and `docs/overview.md` name the file, in the `gh attestation verify` command.
- **The gateway image** builds its own wheel from `pyproject.toml` and `src` with `pip wheel`, then installs `/dist/*.whl` [repo: `docker/Dockerfile.gateway`]. It needs no change once `module-name` is set.
- **The sandbox image** (`docker/Dockerfile`) installs a wheel by glob, so it needs no change either. But a `seatbelt-target` image built before the rename holds no `seatbelt-ai` distribution. There, the new version probe fails with a misleading "cannot import seatbelt" [repo: `src/seatbelt/scenarios/sandbox.py`], so the upgrade note will say to rebuild the image.
- **The README on PyPI.** Its cover image is a relative path (`docs/assets/seatbelt-cover.png`). Its links go to `docs/…`, `examples/…` and root files such as `CHANGELOG.md`, `LICENSE` and `SECURITY.md`. All of them become absolute GitHub URLs, so they work away from GitHub.

## The workflow (ours, following [P1]–[P4])

In the `release` job, a new last step keeps the two distributions:

```yaml
      - uses: actions/upload-artifact@<sha>
        with:
          name: dist
          path: |
            dist/*.whl
            dist/*.tar.gz
```

Then a new job, `pypi`, after `release`:

```yaml
  pypi:
    needs: release           # tests passed, the GitHub release exists
    runs-on: ubuntu-latest
    environment: pypi        # required here: see below
    permissions:
      id-token: write        # mandatory for Trusted Publishing [P2]
    steps:
      - uses: actions/download-artifact@<sha>   # the wheel and sdist the release job built
        with: { name: dist, path: dist }
      - uses: pypa/gh-action-pypi-publish@<sha> # release/v1, pinned by SHA like every action here
```

- **Build once, publish separately.** The `release` job already builds and tests. The new step uploads only the wheel and sdist, and `pypi` downloads them. The action's README says to keep building out of the publishing job, and names `upload-artifact` / `download-artifact` as the way to pass the files [P3].
- **The environment is what guards the upload.** The `release` job also holds `id-token: write`, for its Sigstore attestations. "Any workflow defined in a repository can request an OIDC token, with any audience, so long as it has the id-token: write permission" [P4]. So a separate job alone would not stop the build job from minting a PyPI token. What stops it is that the PyPI publisher names the `pypi` environment, and only the `pypi` job runs in it. The environment is therefore required in both the job and the publisher (ours), though PyPI calls it optional [P2].
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

**On GitHub,** create the `pypi` environment (Settings → Environments). It must be named in the publisher, as above. The security model recommends a dedicated environment, and required reviewers are one of the protections it allows [P4]. With a required reviewer, each PyPI upload waits for an approval in the Actions tab (Decided, 4).

**The names are free.** `seatbelt-ai`, `seatbelt_ai` and the alternatives returned 404 on PyPI's JSON and simple indexes and on TestPyPI, on 2026-09-29 [P0]. A 404 means no public project exists. It does not prove PyPI will accept the name.

## Testing (ours)

- **A test** that `seatbelt.__version__` matches `pyproject.toml`, so the rename cannot break the version silently. After the rename, `version("seatbelt")` raises `PackageNotFoundError`, as a copy of the renamed build showed.
- **Locally, before the release:**
  - `uv build`;
  - `unzip -l` the wheel, to check it holds only `seatbelt/`;
  - install it into a fresh venv, then run `seatbelt version` and `seatbelt demo`.
- **The first publish is the test of the workflow,** unless a dry run on TestPyPI comes first. TestPyPI is supported with `repository-url` and its own publisher [P2][P3]. There is no dry run (Decided, 3).
- **After the release:** `uv tool install seatbelt-ai` on a clean machine, `seatbelt version`, and the attestations shown on the PyPI file page.

## Decided (2026-09-29)

1. **Distribution name: `seatbelt-ai`.**
2. **Module and command: stay `seatbelt`.** The docs recommend an isolated install.
3. **No TestPyPI dry run.** The first real release (0.5.0) is the test, and a failed upload can be re-run. A dry run would need a second pending publisher on test.pypi.org and a tag or manual trigger that publishes there.
4. **A required reviewer on the `pypi` environment**, as the security model suggests [P4]. Each PyPI upload waits for the maintainer's approval.
5. **Existing installs from git.** Those installs are a uv tool named `seatbelt`, which owns the `seatbelt` executable, so installing `seatbelt-ai` beside it fails with "Executable already exists … use `--force`" (checked with uv 0.8.17, above). The upgrade note says to run `uv tool uninstall seatbelt`, then `uv tool install seatbelt-ai`.

## Source map

| Ref | Source | Used for |
|---|---|---|
| P0 | pypi.org/pypi/seatbelt/json and its 0.1.4 wheel, read 2026-09-29; pypi.org/pypi/{seatbelt-ai, seatbelt-agents, seatbelt-ledger, agent-seatbelt}/json returned 404 | the name is taken; the other project's module and command; the alternatives are free |
| P1 | docs.pypi.org/trusted-publishers/creating-a-project-through-oidc | pending publishers create the project on first use; they do not reserve the name, and are invalidated if it is taken first |
| P2 | docs.pypi.org/trusted-publishers/using-a-publisher | the job layout; `id-token: write` mandatory, at job level; environment optional but strongly encouraged; no username, password or token; TestPyPI with `repository-url` |
| P3 | github.com/pypa/gh-action-pypi-publish, README (unstable/v1) | keep building out of the publishing job; pass files with upload/download-artifact; attestations on by default with Trusted Publishing; not from reusable workflows; Trusted Publishing cannot be tested in CI |
| P4 | docs.pypi.org/trusted-publishers/security-model | job-level permissions; a dedicated environment and its protections, e.g. required reviewers |
| P5 | docs.pypi.org/attestations/producing-attestations | with pypa/gh-action-pypi-publish, attestations are generated and uploaded by default |
| U1 | docs.astral.sh/uv/concepts/tools | "When installing a tool with uv tool install, a virtual environment is created in the uv tools directory"; tool executables are linked into one executable directory; uv will not overwrite executables it did not install, and `--force` overrides that; "the required package is inferred from the command name" |
| U2 | docs.astral.sh/uv/guides/tools | uvx infers the package from the command; `--from` runs a command from another package |
| repo | at `b2236a7`: `pyproject.toml`, `src/seatbelt/__init__.py`, `src/seatbelt/scenarios/sandbox.py`, `src/seatbelt/cli.py`, `.github/workflows/release.yml`, `docker/Dockerfile.gateway`, `docker/Dockerfile`, `README.md`, `CHANGELOG.md`, `docs/overview.md` | what names the distribution today |
| checks | 2026-09-29, in scratch copies of `b2236a7`: `uv build` after the rename, with and without `module-name`; uv 0.8.17, two uv tools exporting one command; a second reader's check of this document | the build failure and its fix; the command clash |
