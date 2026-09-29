# Dependency floors and the gateway image's requirements (0.5.2): design

**Goal:**
- `seatbelt-ai` installs beside the agent frameworks people already use.
- Every version its dependency floors allow has been tested and has no known advisory.
- A Dependabot PR no longer fails until someone regenerates a file by hand.

**Status:** a design for review. Facts come from the checks and sources in the source map, run and read 2026-09-29. Decisions that are ours are marked "(ours)".

## The problems

1. **The floors are the latest releases, not the oldest that work.**
   - Every runtime floor in `pyproject.toml` equals the version in `uv.lock` [repo].
   - Dependabot raises them each week: #15 (`815ecab`) moved `starlette>=1.6` to `>=1.7.0`, `uvicorn>=0.53` to `>=0.54.0` and `anthropic>=1.5.0` to `>=1.8.0` [repo].
   - The default strategy for Dependabot is `auto`: "For apps, always increase the minimum version requirement to match the new version", and for libraries it widens instead [D1].
2. **This breaks users who import seatbelt into an agent project.**
   - CrewAI 1.15.23 requires `pydantic<2.13,>=2.11.9` [P1]; seatbelt 0.5.1 requires `pydantic>=2.13.5`.
   - Resolving `seatbelt-ai==0.5.1` with `crewai` picks **crewai 0.10.0**, a years-old release, with no error. With `crewai>=1` there is no solution [check C1].
3. **The gateway image's requirements are a committed file that drifts.**
   - `docker/requirements-gateway.txt` is generated from `uv.lock`, and CI fails when the two differ [repo].
   - Dependabot's own edit to that file did not match the lock: `5255f0c` bumped `pydantic-core` past what the locked pydantic needs. A manual commit (`52e8e3f`) fixed it [repo].
   - So a dependency PR, a security fix included, can sit red until someone regenerates the file.

## Floors (ours, from checks C2–C4)

Rule: **a floor is the highest of**
- the oldest release the test suite passes with (C2);
- the first release from which no later release has a known advisory in OSV (C3). An old version with a published vulnerability is not offered to anyone who installs seatbelt.

| Package | Now | Proposed | What sets it |
|---|---|---|---|
| anyio | 4.15 | **4.14.2** | advisories before 4.14.2 |
| cryptography | 50.0.1 | **50.0.0** | advisories before 50.0.0 |
| httpx2 | 2.13.1 | **2.12.0** | advisories before 2.12.0 |
| pydantic | 2.13.5 | **2.12.2** | tested lowest; the SDK extras need 2.12.2 |
| pyjwt | 2.15 | **2.13.0** | advisories before 2.13.0 |
| pyyaml | 6.0.3 | **6.0.2** | the first with Python 3.13 wheels (6.0.1 has none) [P2] |
| rich | 15.0.0 | **13.8** | tested lowest |
| starlette | 1.7.0 | **1.3.1** | advisories before 1.3.1 |
| typer | 0.27.2 | **0.17** | tested lowest |
| uvicorn | 0.54.0 | **0.34** | tested lowest |
| anthropic (extra) | 1.8.0 | **1.0.0** | tested lowest |
| openai-agents (extra) | 0.22.3 | **0.19** | tested lowest |

The same floors apply to the `gateway` extra and the dev group, whose entries repeat them.

**What the check showed (C2, C4):**
- **The floors are a fixed point.** With these floors, `uv lock --resolution lowest-direct` installs exactly these versions.
- **Tests:** 660 unit tests pass; the 7 failures are the known root-only ones.
- **pyright:** it reports 30 errors at these versions. They come from older libraries' type information, typer's among them, and there are no runtime failures. So pyright keeps running on the locked versions only.
- **CrewAI:** `seatbelt-ai[anthropic,openai-agents]` built with these floors resolves beside `crewai>=1` to crewai 1.15.23 and pydantic 2.12.5.

The biggest gains are the packages that frameworks also pin: pydantic, rich, typer, uvicorn. The security-relevant ones stay recent.

No upper bounds are added (ours). A cap on a library's dependencies blocks users' security updates, and the lowest-versions job below guards the other end.

## A CI job at the floors (ours)

A new `lowest` job in `ci.yml`, on Python 3.12 and 3.13:

```yaml
      - run: uv lock --resolution lowest-direct  # direct deps at their floors, the rest latest [U1]
      - run: uv sync --frozen
      - run: uv run --frozen pytest -m "not integration"
      - name: No known vulnerability at the floors
        run: |
          uv export --frozen --no-dev --all-extras --no-emit-project -o lowest.txt
          uvx pip-audit==2.10.1 -r lowest.txt --require-hashes --disable-pip
```

- **The tests.** uv's docs recommend separately testing with `--resolution lowest-direct` in CI when publishing a library, "to ensure compatibility with the declared lower bounds" [U1].
- **The audit.** `pip-audit` exits 1 when it finds an advisory (C5), so a new advisory against a floor fails the job, and whoever raises that floor sees why.
  - C5 ran it on the proposed floors: "No known vulnerabilities found".
  - It also ran on `starlette==0.49.1, pyjwt==2.10.1`: it listed pyjwt's advisories, fixed in 2.13.0, and exited 1.
- **No pyright,** for the reason above.
- **Python 3.13** was not available in this container. The 3.13 run is CI's first.

## Dependabot (ours, following D1)

For the `uv` ecosystem, set `versioning-strategy: lockfile-only`: "Only create pull requests to update lockfiles. Ignore any new versions that would require package manifest changes" [D1].
- The floors are `>=` with no upper bound, so every new release fits them. Dependabot then updates `uv.lock` and leaves `pyproject.toml` alone.
- A floor moves only by hand, when code needs a newer API or the audit reports an advisory. CONTRIBUTING will say so.

## The gateway image builds its own requirements (ours, following U2, U3)

The build stage gets uv from its official image, pinned by tag and digest, as uv's Docker guide recommends [U2]:

```dockerfile
FROM ghcr.io/astral-sh/uv:0.12.20@sha256:100047e74f30778ab704942321a09750d6158739573ff58bf3924085cc6cd2d8 AS uv
FROM python:3.12-slim@sha256:... AS build
COPY --from=uv /uv /bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv export --locked --offline --no-dev --extra gateway --no-emit-project -o /requirements.txt \
    && uv build --wheel --offline -o /dist
```

The runtime stage stays as it is: `pip install --require-hashes` of those requirements, then the wheel with `--no-deps`.

**What this removes:**
- `docker/requirements-gateway.txt`;
- `docker/requirements-build.txt`;
- the CI step that compares the first with `uv.lock`;
- their `.dockerignore` entries (`uv.lock` is added instead).

**Why it holds (C6, uv 0.12.20, the release checksum verified):**
- **No network and an empty cache.** `uv export --locked --offline` writes the same pins as the committed file; only the header comment naming the command differs.
- **Nothing is fetched to build.** `uv build --wheel --offline` logs "Using bundled `uv_build` backend". The uv binary carries the build backend and uses it "if its version is compatible with the `uv_build` requirement" [U3].
- **A mismatch fails the build.** With the `uv_build` range changed so the bundled one no longer fits, the offline build fails with "Packages were unavailable because the network was disabled". Nothing unpinned can slip in.
- **The image's dependencies come from `uv.lock` alone,** so a Dependabot PR that updates the lock rebuilds the image from it with nothing else to regenerate.

**Keeping uv in step:**
- The workflows pin uv 0.12.20 by version (and by checksum where the release is built), and `pyproject.toml` requires `uv_build>=0.12.19,<0.13.0`.
- The image's uv stage joins them: all three move together, by hand. CONTRIBUTING's release step 4 already says so for the first two.
- Dependabot's docker updates would otherwise move the uv stage on their own. An `ignore` entry for `ghcr.io/astral-sh/uv` minor and major updates keeps them to patch digests (ours).
- Whether Dependabot updates a `COPY --from` stage image at all is not checked here: [UNVERIFIED].

## Testing (ours)

- **The `lowest` job** on this PR: its tests and its audit must pass on 3.12 and 3.13.
- **The image:**
  - CI's `sandbox` job builds the gateway image from the new Dockerfile;
  - a local run then compares its installed packages with `uv export` of the lock.
- **A resolution test** (C1's check) in `tests/`: `seatbelt-ai` built from the tree resolves beside `crewai>=1`. It would need the network, so it is a CI-only job, not a unit test. Open, 2.
- **After the release,** a Dependabot PR must go green with no manual commit.

## Open

1. **Floors on the dev group.** They only matter to contributors. Recommendation: keep them equal to the runtime ones, for one rule.
2. **The CrewAI resolution check in CI.** It guards the concrete case that started this, but ties CI to another project's releases. Recommendation: skip it; the floors and the `lowest` job are the guard.
3. **A weekly scheduled run of the `lowest` job,** so a new advisory against a floor is seen without waiting for a PR. Recommendation: yes; it costs one job a week.

## Source map

| Ref | Source | Used for |
|---|---|---|
| D1 | docs.github.com, Dependabot options reference, `versioning-strategy` (read 2026-09-29): "Supported by: … `pip`, `pub`, and `uv`"; the `auto`/`increase`/`lockfile-only` behaviours quoted above | why the floors rise; the fix |
| U1 | astral-sh/uv docs at tag 0.12.20, `docs/concepts/resolution.md`: `--resolution lowest-direct` "will use the lowest compatible versions for all direct dependencies, while using the latest compatible versions for all other dependencies"; "When publishing libraries, it is recommended to separately run tests with `--resolution lowest` or `--resolution lowest-direct` in continuous integration" | the CI job |
| U2 | the same, `docs/guides/integration/docker.md`: `COPY --from=ghcr.io/astral-sh/uv:0.12.20 /uv /uvx /bin/`; "Pinning a specific SHA256 is considered best practice … as tags can be moved" | the uv stage |
| U3 | the same, `docs/concepts/build-backend.md`: the uv executable "includes a copy of the build backend, which will be used during builds performed by uv, e.g., during `uv build`, if its version is compatible with the `uv_build` requirement" | building without a pinned backend package |
| P1 | pypi.org/pypi/crewai/json, 1.15.23: `pydantic<2.13,>=2.11.9`, `pyjwt<3,>=2.13.0` | the clash |
| P2 | pypi.org JSON for pyyaml 6.0.1 (no `cp313` file) and 6.0.2 (`cp313` files) | the pyyaml floor |
| G1 | ghcr.io registry, `astral-sh/uv` manifest for tag 0.12.20: `sha256:100047e7…2d8` | the digest pin |
| repo | at `43187cb`: `pyproject.toml`, `uv.lock`, `.github/dependabot.yml`, `.github/workflows/ci.yml`, `docker/Dockerfile.gateway`, `docker/requirements-*.txt`; commits `815ecab`, `5255f0c`, `52e8e3f` | the current setup and its history |
| C1 | `uv pip compile` of `seatbelt-ai==0.5.1` with `crewai`, and with `crewai>=1`, Python 3.12 | crewai 0.10.0; unsatisfiable |
| C2 | a copy of `43187cb` with the proposed floors: `uv lock --resolution lowest-direct`, `uv sync --frozen`, the installed versions, `pytest tests/unit` (660 passed, 7 root-only failures), `pyright` (30 errors) | tested floors; the fixed point |
| C3 | OSV batch queries (api.osv.dev) for every release from each trial floor to the latest | the security floors |
| C4 | `uv pip compile` of the C2 wheel with extras beside `crewai>=1` | crewai 1.15.23 resolves |
| C5 | `pip-audit` 2.10.1 on `uv export` of C2's lock (clean), of the current lock (clean), and on `starlette==0.49.1`, `pyjwt==2.10.1` (advisories listed, exit 1) | the audit step |
| C6 | uv 0.12.20 release binary (checksum verified) with an empty cache: `uv export --locked --offline` against the committed file; `uv build --wheel --offline -v`; the same build with the `uv_build` range moved to `>=0.13.0` | building the image's inputs in the image |
