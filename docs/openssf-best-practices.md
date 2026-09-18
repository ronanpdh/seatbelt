# OpenSSF Best Practices badge: evidence for the passing level

Register the project at <https://www.bestpractices.dev/en/projects/new> with the repository URL, then answer each criterion from this table. Criteria are the passing level as published at <https://www.bestpractices.dev/en/criteria/0>. `Met` means the repository already satisfies it; the evidence column is what to paste.

## Basics

| Criterion | Status | Evidence |
|---|---|---|
| description_good | Met | README first paragraph |
| interact | Met | README "Try it in five minutes", "Project" section: issues, CONTRIBUTING.md |
| contribution | Met | CONTRIBUTING.md: setup, checks, pull request rules |
| contribution_requirements | Met | CONTRIBUTING.md "Changes" and "Tests"; STANDARDS.md |
| floss_license, floss_license_osi | Met | Apache-2.0, `LICENSE`, `pyproject.toml` `license` |
| license_location | Met | `LICENSE` at the repository root |
| documentation_basics | Met | README, `docs/adr/`, `docs/spec/`, `docs/schema/` |
| documentation_interface | Met | README "Commands" table, every command's `--help`, `docs/spec/evidence-pack-v1.md`, `docs/schema/*.json` |
| sites_https | Met | github.com only |
| discussion | Met | GitHub issues (searchable, per-issue URLs) |
| english | Met | all documentation in English |
| maintained | Met | commit history, CHANGELOG dates |

## Change control

| Criterion | Status | Evidence |
|---|---|---|
| repo_public, repo_track, repo_interim, repo_distributed | Met | public git repository on GitHub, every commit on `main` |
| version_unique, version_semver, version_tags | Met | SemVer in `pyproject.toml`, tags `vX.Y.Z`, `release.yml` refuses a tag that does not match |
| release_notes | Met | CHANGELOG.md (Keep a Changelog); each GitHub release uses its section as notes |
| release_notes_vulns | Met | CHANGELOG has a `### Security` heading where applicable (see 0.0.3) |

## Reporting

| Criterion | Status | Evidence |
|---|---|---|
| report_process, report_tracker, report_archive | Met | CONTRIBUTING.md "Bugs and ideas", GitHub issues |
| report_responses, enhancement_responses | Ongoing | answer issues; the badge form asks for the majority of the last 2 to 12 months |
| vulnerability_report_process | Met | SECURITY.md |
| vulnerability_report_private | Met | SECURITY.md: email address, no public issues |
| vulnerability_report_response | Met | SECURITY.md: acknowledgement within 3 working days (limit is 14 days) |

## Quality

| Criterion | Status | Evidence |
|---|---|---|
| build, build_common_tools, build_floss_tools | Met | `uv sync`, `uv build`; uv, ruff, pyright, pytest are all FLOSS |
| test, test_invocation | Met | `uv run pytest`, `tests/unit/` |
| test_most | Met | unit, property (Hypothesis) and CLI tests over every module; tamper matrices for ledger, attestation and pack |
| test_continuous_integration | Met | `.github/workflows/ci.yml` on push and pull request, Python 3.12 and 3.13 |
| test_policy, tests_are_added, tests_documented_added | Met | CONTRIBUTING.md "Tests"; every feature commit adds tests |
| warnings, warnings_fixed, warnings_strict | Met | ruff (with bandit rules) and pyright strict in pre-commit and CI; zero findings on `main` |

## Security

| Criterion | Status | Evidence |
|---|---|---|
| know_secure_design, know_common_errors | Met | SECURITY.md "Design" |
| crypto_published, crypto_call, crypto_floss, crypto_keylength, crypto_working, crypto_weaknesses | Met | SECURITY.md "Cryptography": SHA-256 and Ed25519 via `cryptography` |
| crypto_pfs | N/A | no key agreement protocol |
| crypto_password_storage | N/A | no passwords stored |
| crypto_random | Met | keys from `Ed25519PrivateKey.generate()` (library CSPRNG) |
| delivery_mitm, delivery_unsigned | Met | HTTPS to GitHub and PyPI; releases carry Sigstore signatures and SLSA provenance (`release.yml`) |
| vulnerabilities_fixed_60_days, vulnerabilities_critical_fixed | Met | SECURITY.md commitment; Dependabot weekly (`.github/dependabot.yml`) |
| no_leaked_credentials | Met | pre-commit `detect-private-key`; `*.key` and `keys/` in `.gitignore`; redaction before write |

## Analysis

| Criterion | Status | Evidence |
|---|---|---|
| static_analysis, static_analysis_often | Met | ruff and pyright on every push and pull request |
| static_analysis_common_vulnerabilities | Met | ruff `S` (bandit) rules enabled; OpenSSF Scorecard weekly (`.github/workflows/scorecard.yml`) |
| static_analysis_fixed | Met | zero findings on `main` |
| dynamic_analysis, dynamic_analysis_enable_assertions | Met | Hypothesis property tests run under pytest with assertions enabled |
| dynamic_analysis_unsafe | N/A | Python |
| dynamic_analysis_fixed | Met | nothing outstanding |

After the badge is issued, add to README under the Scorecard badge:

```markdown
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/<id>/badge)](https://www.bestpractices.dev/projects/<id>)
```
