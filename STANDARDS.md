# Engineering standards

These are the rules the harness is built to. They are opinionated on purpose.

## Language and runtime

- Python 3.12 or newer. One supported minor version behind at most.
- Type hints everywhere. `pyright` in strict mode is part of CI. `Any` needs a comment.
- Async by default for anything that talks to a model, a tool or a sandbox.

## Project layout

```
harness/
  pyproject.toml          single source of metadata, deps and tool config
  uv.lock                 committed
  src/seatbelt/      src layout, importable package
    ledger/               Event model, hash-chained JSONL store, redaction
    record/               Recorder API that adapters call
    adapters/             one module per framework, translate only
    policy/               rules and engine at the tool boundary (v0.0.3)
    verify/               chain and signature verification
    attest/               manifest and signing (v0.0.4)
    report/               timeline and evidence pack
    scenarios/            adversarial scenario pack (v0.1.0)
    cli.py                Typer entry point
  tests/
    unit/
    integration/          needs Docker, skipped without it
    fixtures/
  docs/
    adr/                  one file per decision, numbered
  log/                    weekly learning log (repo root)
  scenarios/              shipped YAML scenario corpus
```

Rationale for src layout: https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/

## Tooling

| Purpose | Tool | Docs |
|---|---|---|
| Env, deps, build | uv | https://docs.astral.sh/uv/ |
| Lint and format | ruff | https://docs.astral.sh/ruff/ |
| Types | pyright (strict) | https://microsoft.github.io/pyright/ |
| Tests | pytest | https://docs.pytest.org/en/stable/ |
| Property tests | Hypothesis | https://hypothesis.readthedocs.io/en/latest/ |
| Hooks | pre-commit | https://pre-commit.com/ |
| Data models | Pydantic | https://docs.pydantic.dev/latest/ |
| CLI | Typer | https://typer.tiangolo.com/ |
| Terminal output | Rich | https://rich.readthedocs.io/en/stable/ |
| Logging | structlog, JSON to stderr | https://www.structlog.org/en/stable/ |
| Report templates | Jinja2 | https://jinja.palletsprojects.com/en/stable/ |
| Project template | copier-uv | https://github.com/pawamoy/copier-uv |

## Code rules

- Every scenario, finding, target and run is a Pydantic model with a JSON schema exported to `docs/schema/`. Schemas are versioned.
- No finding without evidence. A `Finding` must reference at least one ledger event id. The type system enforces it.
- Nothing reaches the ledger except through `Recorder._emit`, and `_emit` always redacts first.
- Deterministic replay. A run must be reconstructable from its trace alone, with model responses replayed from the recording. Tests use recorded traces, never live models.
- Adapters are thin. They translate one framework's events into the harness trace schema and nothing else. Framework SDKs are optional dependencies.
- Sandboxed by default. Targets run in Docker with no network unless a scenario declares egress. Pattern: https://inspect.aisi.org.uk/sandboxing.html
- Redaction before persistence. Secrets, tokens and personal data are scrubbed from traces and packs. Redaction is tested.
- Pin what you test. A run records the exact model identifier and version string, adapter version, harness version, corpus hash and sandbox image digest.
- Trace schema follows OpenTelemetry GenAI semantic conventions, not a home-grown one: https://github.com/open-telemetry/semantic-conventions-genai
- Use the standard threat vocabulary. Every scenario maps to at least one OWASP Agentic Top 10 item and, where one exists, a MITRE ATLAS technique.

## Testing

- Unit tests do not touch the network or Docker.
- Integration tests are marked and skipped when Docker is absent.
- Attack corpora have golden-file tests so a change to a scenario shows up in review.
- Property tests on parsers, redaction and schema round-trips.
- Coverage is reported, not gated. A gate on a number invites bad tests.

## Git and releases

- Conventional Commits: https://www.conventionalcommits.org/en/v1.0.0/
- Semantic Versioning: https://semver.org/
- Keep a Changelog: https://keepachangelog.com/en/1.1.0/
- Architecture Decision Records in `docs/adr/`: https://adr.github.io/
- Main is protected. Everything lands by PR, including your own work, even when you are the only contributor. It builds the habit and the history.
- Releases are tagged, built by CI, signed with Sigstore, and ship a CycloneDX SBOM.

## Supply chain and security posture

The harness is a security tool. It has to be held to the standard it measures others against.

- SECURITY.md with a disclosure route (see the repo root): https://docs.github.com/en/code-security/getting-started/adding-a-security-policy-to-your-repository
- OpenSSF Scorecard runs in CI: https://scorecard.dev/
- Aim for the OpenSSF Best Practices badge by v0.1.0: https://www.bestpractices.dev/en
- SLSA build provenance from GitHub Actions: https://slsa.dev/
- Sigstore for release signing: https://docs.sigstore.dev/
- CycloneDX SBOM per release: https://cyclonedx.org/
- Dependency and code scanning in CI: pip-audit (https://pypi.org/project/pip-audit/), Bandit (https://bandit.readthedocs.io/en/latest/), Semgrep (https://github.com/semgrep/semgrep-rules), Trivy on container images (https://trivy.dev/)
- Dependencies are pinned through `uv.lock`. Updates arrive as PRs, reviewed like code.

## Licence and community

- Licence: Apache-2.0 (permissive, includes an explicit patent grant). Compare options: https://choosealicense.com/licenses/apache-2.0/
- Code of conduct: Contributor Covenant: https://www.contributor-covenant.org/
- Contribution guide: `CONTRIBUTING.md`

## Documentation

- README answers three questions in the first screen: what it tests, what it produces, how to run it in five minutes.
- Every public function has a docstring. Every ADR has context, decision, consequences.
- The evidence pack format is documented as a spec with a version, because a CISO will ask what a field means.
