# Building seatbelt

How seatbelt was built, one release at a time, and where the build went differently from the plan.

The build guide was written on 2026-09-14, the day 0.0.1 was released. It gave 0.0.1 step by step, with every file, and 0.0.2 to 0.1.0 as short guided plans. A separate adapters guide, written the same day, gave 0.0.2's Anthropic adapter in full. Neither covered anything after 0.1.0. This file keeps those plans and sets beside them what actually shipped, through 0.5.0. Each release has:

- **Planned**: what the guide said to build (0.0.1 to 0.1.0), or the design note it was built from (0.2.0 on).
- **Shipped**: what the release contains. The full list is in [CHANGELOG.md](../CHANGELOG.md).
- **Plan vs repo**: where the two differ.
- **Read**: the ADRs and design notes behind it.
- **Check**: commands that show it working. Run from a checkout after `uv sync --all-extras`. Every check was run against 0.5.0 on 2026-09-29, except where a line says it was not.
- **Log**: its entry in the [learning log](../log/).

The sources for every statement are listed in the [source map](#source-map) at the end.

## At a glance

| Release | Date | What it added |
|---|---|---|
| [0.0.1](#001-a-run-you-can-verify-and-reconstruct) | 2026-09-14 | hash-chained ledger, redaction, `Recorder`, `verify`, `reconstruct` |
| [0.0.2](#002-real-agents-through-adapters) | 2026-09-15 | Anthropic and OpenAI Agents SDK adapters, ledger schema version, signed releases |
| [0.0.3](#003-policy-at-the-tool-boundary) | 2026-09-17 | policy engine at the tool boundary, ledger hardening |
| [0.0.4](#004-signed-runs) | 2026-09-17 | Ed25519-signed run manifest |
| [0.1.0](#010-attack-it-sandbox-it-hand-it-over) | 2026-09-18 | scenario pack, Docker sandbox, evidence pack |
| [0.2.0](#020-record-an-organisation-through-a-gateway) | 2026-09-28 | recording gateway, launcher, fleet report |
| [0.3.0](#030-every-major-client-and-recording-with-no-gateway) | 2026-09-29 | Responses and Gemini formats, local recording, OIDC, config reload, sink, Compliance API importer |
| [0.4.0](#040-one-row-per-person-and-erasure) | 2026-09-29 | one row per person in `report`, `seatbelt erase` |
| [0.5.0](#050-on-pypi) | 2026-09-29 | published to PyPI as `seatbelt-ai` |

## Modules by release

What each release tag added under `src/seatbelt/` (`__init__.py` files left out). There is no `v0.0.1` tag, so the first row is the tree at `v0.0.2`.

| Tag | Added | Removed |
|---|---|---|
| `v0.0.2` | `cli.py`, `ledger/{events,store,redact}.py`, `record/recorder.py`, `verify/chain.py`, `report/timeline.py`, `adapters/{base,anthropic,openai_agents}.py` | |
| `v0.0.3` | `policy/engine.py` | `adapters/base.py` |
| `v0.0.4` | `attest/{manifest,sign}.py`, `verify/attest.py` | |
| `v0.1.0` | `scenarios/{model,checks,runner,sandbox,child}.py`, `report/pack.py` | |
| `v0.2.0` | `gateway/{app,config,sessions,serve,launcher}.py`, `gateway/formats/{anthropic,openai_chat}.py`, `report/fleet.py` | |
| `v0.3.0` | `gateway/formats/{openai_responses,gemini}.py`, `gateway/{local,oidc,reload,sink}.py`, `compliance/{client,importer,mapping}.py` | |
| `v0.4.0` | `erase.py`, `locks.py` | |
| `v0.5.0` | | |

Every package in the guide's 0.0.1 layout (`adapters`, `ledger`, `record`, `report`, `verify`) is still there.

---

## 0.0.1: a run you can verify and reconstruct

2026-09-14.

**Planned.**
- An event model: `Event`, `Actor` and `Kind`, hashed as canonical JSON.
- An append-only JSONL `Ledger` with a SHA-256 chain per run, which resumes after a restart.
- Redaction before anything is hashed or written.
- A `Recorder` that owns actor identities and parent links.
- `verify_events` and `verify_file`, returning a `Verdict` that names the first bad sequence number.
- A Rich timeline, and the CLI commands `version`, `verify`, `reconstruct` and `demo`.
- 11 tests, one of them a Hypothesis property test that any edit breaks the chain.

The guide also listed six ADRs to write before coding, numbered 0002 to 0007: append-only event sourcing, a hash chain per run, redact before hash, OpenTelemetry GenAI attribute names, kinds from the scope chain, and JSONL with one file per run.

**Shipped.** All of the code above.

**Plan vs repo.**
- Four of the six planned ADRs became one. [ADR 0001](adr/0001-hash-chained-jsonl-ledger.md) covers the append-only JSONL file per run, the hash chain, redaction before hashing, and verification. The other two rules live in docstrings in `src/seatbelt/ledger/events.py`, not in ADRs: OpenTelemetry attribute names in `Event`, and kinds from the scope chain in `Kind`.
- The 0.0.1 code has changed since:
  - 0.0.2: every event carries a required `schema_version`.
  - 0.0.3: `Event` and `Actor` forbid unknown keys. Ledgers are created mode 0600 and fsynced. `Ledger.read` became `read_events(path)`.

**Read.** [ADR 0001](adr/0001-hash-chained-jsonl-ledger.md).

**Check.**

```sh
uv run seatbelt demo --out /tmp/runs
uv run seatbelt verify /tmp/runs/<run id>.jsonl       # ok 11 events, chain intact, unattested
uv run seatbelt reconstruct /tmp/runs/<run id>.jsonl
sed -i 's/refund issued/refund denied/' /tmp/runs/<run id>.jsonl   # macOS: sed -i ''
uv run seatbelt verify /tmp/runs/<run id>.jsonl       # BROKEN at seq 9: content does not match its hash (exit 1)
```

**Log.** Not in the repo yet.

## 0.0.2: real agents through adapters

2026-09-15.

**Planned.**
- An `Adapter` protocol with one method, `attach(rec)`.
- An Anthropic Messages adapter that wraps `client.messages.create`. It records the model version the provider returned, and links each `tool_result` block to its `tool_use` block by id.
- `Recorder.tool_called` and `Recorder.tool_returned`, for adapters that see a tool call and its result in separate requests.
- A recorded fixture, replayed through a fake client, and `examples/anthropic_refund.py`.
- An OpenAI Agents SDK adapter, built as a tracing processor.
- ADR-0008: adapters translate, and never interpret.

**Shipped.** All of the above except ADR-0008, plus:
- Ledger schema version 1. Every event carries `schema_version`, and 0.0.1 ledgers are rejected as an old format.
- A release workflow. Each tagged release gets the wheel, sdist and a CycloneDX SBOM, with Sigstore-signed SLSA build provenance and SBOM attestations.
- `seatbelt verify` reports INCOMPLETE (exit 1) when the chain is intact but does not end in a `run.end` with a matching event count.
- `examples/anthropic_walkthrough.py`, a live run through every event kind.

**Plan vs repo.**
- ADR-0008 was not written. The repo's ADRs are numbered 0001 to 0007, and none of them is about adapters.
- The `Adapter` protocol and `AnthropicAdapter.attach` shipped, and were removed in 0.0.3. Nothing consumed the protocol, and `attach` did not affect wrappers already created.
- Signed releases arrived in 0.0.2. The setup guide had placed them in week 15.

**Read.** [CHANGELOG 0.0.2](../CHANGELOG.md#002---2026-09-15).

**Check.**

```sh
uv run pytest tests/unit/test_anthropic_transports.py tests/unit/test_openai_agents_adapter.py -q
uv run python examples/anthropic_refund.py   # needs ANTHROPIC_API_KEY; not run for this guide
```

**Log.** Not in the repo yet.

## 0.0.3: policy at the tool boundary

2026-09-17.

**Planned.**
- Rules loaded from `policies/*.yaml`. Each rule has a tool-name glob, an effect (`allow`, `deny` or `require_approval`) and a condition over the arguments.
- `evaluate(rule_set, tool_name, arguments)`, recording a `policy.check` event linked to the `tool.call` it judged. A denied call never reaches the tool.
- `require_approval` records a `decision` with the approver as its authority.
- ADR-0009: fail closed.

**Shipped.**
- `seatbelt.policy.engine`. `Recorder.start(..., policy=Policy(*rules))` evaluates every rule on each `Recorder.tool_call`, records one `policy.check` per rule linked to the call, and raises `PolicyDenied` before the tool body runs if any rule denies. A rule that raises counts as a denial, and is recorded as one. The built-in rules are `allowlist` and `denylist`.
- Ledger hardening: unknown keys rejected, redaction anchored to token shapes, files created 0600 and fsynced, thread-safe appends. `Recorder.start` refuses an existing ledger or an unsafe run id.
- The Anthropic adapter covers `messages.stream()`, `AsyncAnthropic` and the beta endpoint.
- `seatbelt reconstruct` verifies first, and refuses a tampered ledger.

**Plan vs repo.**
- A rule is a name and a Python callable, `(tool, arguments) -> reason to deny, or None`. Rules are not YAML files, and there is no `require_approval` effect. Org policy did get YAML later, as the gateway config's `policy:` block (0.2.0).
- Fail closed shipped as planned, recorded in the changelog and `Policy.evaluate`'s docstring rather than in ADR-0009.
- The adapters only observe tool calls that the framework dispatches, so they record no policy checks. Enforcement goes through `Recorder.tool_call`.

**Read.** [CHANGELOG 0.0.3](../CHANGELOG.md#003---2026-09-17), `src/seatbelt/policy/engine.py`.

**Check.**

```sh
uv run pytest tests/unit/test_policy.py -q
```

**Log.** [log/2026-09-17-v0.0.3.md](../log/2026-09-17-v0.0.3.md).

## 0.0.4: signed runs

2026-09-17.

**Planned** (as "attestation and the evidence pack").
- At `run.end`, a manifest holding the harness version, event count, first and last hash, every distinct actor, and the ledger's SHA-256.
- The manifest signed keyless with Sigstore, and wrapped in an in-toto Statement.
- `seatbelt verify --signed`.
- A Jinja2 HTML evidence report, printed to PDF.
- ADR-0010.

**Shipped.**
- `seatbelt.attest`: an Ed25519-signed manifest in `<run id>.attest.json`, written at run end, failed runs included. It holds the final hash, event count, schema version, run id and the ledger file's SHA-256.
- `seatbelt keygen`, and `seatbelt attest <ledger> --key` to sign after the fact.
- `seatbelt verify --pubkey`, which reports ATTESTED, FORGED (exit 1), UNCHECKED (a sidecar but no key) or UNATTESTED.

**Plan vs repo.**
- Ed25519 with a local key, not Sigstore. [ADR 0002](adr/0002-signed-run-manifest.md) rejected Sigstore keyless for now, because it needs the network at sign time and a heavy dependency. It names Sigstore as the right next step for cross-organisation trust. HMAC was rejected too, because whoever can verify can forge.
- No in-toto Statement around run manifests. In-toto is used for release provenance instead: SLSA build provenance attestations from 0.0.2, also attached as the `.intoto.jsonl` release asset from 0.3.0.
- The flag is `--pubkey`, not `--signed`. The reviewer supplies the public key; the one embedded in the manifest is never trusted.
- The evidence pack moved to 0.1.0, as a signed zip rather than an HTML report.
- The ADR is 0002, not 0010.

**Read.** [ADR 0002](adr/0002-signed-run-manifest.md), [attestation design](plans/2026-09-17-attestation-design.md).

**Check.** On a fresh `demo` run (the 0.0.1 check leaves a tampered one, which `attest` refuses):

```sh
uv run seatbelt keygen /tmp/keys
uv run seatbelt attest /tmp/runs/<run id>.jsonl --key /tmp/keys/seatbelt.key
uv run seatbelt verify /tmp/runs/<run id>.jsonl --pubkey /tmp/keys/seatbelt.pub   # ok 11 events, chain intact, attested
```

**Log.** [log/2026-09-17-v0.0.4.md](../log/2026-09-17-v0.0.4.md).

## 0.1.0: attack it, sandbox it, hand it over

2026-09-18.

**Planned** (as "interoperable").
- An OpenTelemetry export, `export/otel.py`.
- Framework references on each kind and policy rule (the OWASP Agentic Top 10, NIST AI RMF and NCSC guidelines), grouped in the pack.
- An adversarial scenario pack: YAML cases that drive an adapter and assert on the ledger, with findings that cite event ids.
- Release engineering: Scorecard, SBOM, signed release, and the Best Practices badge.
- A launch post, and submissions to the OWASP Agentic Security Initiative and the Inspect community.

**Shipped.**
- Scenario pack, `seatbelt.scenarios` ([ADR 0003](adr/0003-scenario-pack.md)). 8 YAML scenarios under `scenarios/`: 7 adversarial ones, each mapped to the OWASP Top 10 for Agentic Applications and, where one exists, a MITRE ATLAS technique, and a benign control. They run against a `target(rec, inputs)` callable. A closed vocabulary of checks is evaluated over the ledger, and every finding cites the event ids that prove it.
- Docker sandbox ([ADR 0005](adr/0005-docker-sandbox.md)): one hardened container per scenario, with no network unless the scenario sets `egress: true`, and the image digest recorded in `run.start`.
- Evidence pack ([ADR 0004](adr/0004-evidence-pack.md)): `seatbelt pack` and `seatbelt verify-pack`, a zip bound by a signed `pack.json`, specified in [evidence-pack-v1](spec/evidence-pack-v1.md).
- `docs/overview.md`, and OpenSSF Best Practices evidence in `docs/openssf-best-practices.md`.

**Plan vs repo.**
- No OpenTelemetry export, and no NIST AI RMF or NCSC mapping: scenarios carry OWASP and ATLAS ids only. Event attributes still follow the OpenTelemetry GenAI names.
- Scenarios hand a `Recorder` to a target callable rather than drive an adapter. The target decides how its agent is recorded. `examples/anthropic_scenario_target.py` (0.2.0) records through the Anthropic adapter.
- The pack is a signed zip. ADR 0004 rejects rendered PDF or HTML as "a later layer over the same pack".
- The Best Practices evidence landed in 0.1.0. The badge appears in the README from 0.4.0.
- Nothing in the repo records a launch post or the two submissions.

**Read.** ADRs [0003](adr/0003-scenario-pack.md), [0004](adr/0004-evidence-pack.md), [0005](adr/0005-docker-sandbox.md); designs for the [scenarios](plans/2026-09-17-scenarios-design.md), [pack](plans/2026-09-17-evidence-pack-design.md) and [sandbox](plans/2026-09-17-sandbox-design.md); [docs/scenarios.md](scenarios.md).

**Check.** With the key from the 0.0.4 check:

```sh
uv run seatbelt scenarios scenarios/ --list
uv run seatbelt scenarios scenarios/ --target examples.scenario_target:target --out /tmp/scen --key /tmp/keys/seatbelt.key
# 7 PASS, 1 FAIL (indirect-injection-refund, the example's deliberate flaw); exit 1 because there is a finding
uv run seatbelt pack /tmp/scen --out /tmp/audit.seatbelt.zip --key /tmp/keys/seatbelt.key --corpus scenarios/
uv run seatbelt verify-pack /tmp/audit.seatbelt.zip --pubkey /tmp/keys/seatbelt.pub   # ok 8 runs, pack attested
uv run pytest -m integration   # the sandbox against a real Docker daemon; not run for this guide
```

**Log.** [log/2026-09-18-v0.1.0.md](../log/2026-09-18-v0.1.0.md).

## 0.2.0: record an organisation through a gateway

2026-09-28.

**Planned.** Not in the guide. It was designed in [2026-09-18-gateway-design.md](plans/2026-09-18-gateway-design.md). [ADR 0006](adr/0006-recording-gateway.md) gives the reason: until 0.2.0, recording needed the agent's code to call the `Recorder` or wrap an SDK client. Employees run Claude Code, Claude Desktop, Cowork and SDK agents they did not write. So almost nothing was recorded, what was recorded sat on the employee's own disk, and nothing bound a run to a person.

**Shipped.**
- `seatbelt gateway serve`, an HTTP service that clients point their base URL at. It speaks Anthropic Messages and OpenAI Chat Completions, streamed or not. Each wire format is one module under `seatbelt.gateway.formats`, and the Anthropic one is shared with the SDK adapter.
- Identity by issued key. `seatbelt gateway keygen --user <id>` prints a key once and stores only its SHA-256. `run.start` records `principal.id` and `principal.key_id`.
- One ledger per employee session. It is closed and signed on idle, on an explicit end or at shutdown, and never while a request is in flight. On start, the gateway closes and signs any chain a crash left open.
- Org policy. A model not on the allowed list, or a request over the output-token cap, is refused with 403. A denied tool is recorded and relayed, and any later request that carries its result is refused.
- `seatbelt run claude`, the launcher ([ADR 0007](adr/0007-launcher.md)), and `seatbelt report`, the fleet report.
- `docker/Dockerfile.gateway` and the deployment guides in `docs/deploy/`.
- `Recorder.model_requested`, the request half of `model_call`, for a response that arrives later.

**Plan vs repo.**
- The design put a `codex` launcher preset in 0.2.0. `seatbelt run codex` was refused instead, because current Codex speaks only the OpenAI Responses API, which the gateway did not serve until 0.3.0.
- The design rejected SSO "from day one". OIDC sign-in followed in 0.3.0.

**Read.** ADRs [0006](adr/0006-recording-gateway.md) and [0007](adr/0007-launcher.md), the [gateway design](plans/2026-09-18-gateway-design.md), [docs/deploy/gateway.md](deploy/gateway.md).

**Check.** This runs a gateway on localhost with a dummy provider key. The policy refuses the request, so nothing reaches a provider.

```sh
uv run seatbelt keygen /tmp/gw/keys
cat > /tmp/gw/gateway.yaml <<'EOF'
listen: 127.0.0.1:8080
signing_key: keys/seatbelt.key
ledgers: runs
upstreams:
  anthropic: {url: https://api.anthropic.com, key_env: ANTHROPIC_API_KEY}
policy:
  models: [claude-sonnet-5]
EOF
uv run seatbelt gateway keygen --user alice@corp --config /tmp/gw/gateway.yaml   # prints an sbk_ key once
ANTHROPIC_API_KEY=unused uv run seatbelt gateway serve --config /tmp/gw/gateway.yaml &
curl -X POST localhost:8080/v1/messages -H "x-api-key: <sbk key>" -H "anthropic-version: 2023-06-01" \
  -H "X-Seatbelt-Run: check" -d '{"model":"gpt-9","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
# 403: model gpt-9 is not allowed
curl -X POST localhost:8080/seatbelt/runs/check/end -H "x-api-key: <sbk key>"   # 204; the run is closed and signed
kill %1
uv run seatbelt report /tmp/gw/runs --pubkey /tmp/gw/keys/seatbelt.pub   # alice@corp: 1 run, 1 denied
uv run seatbelt reconstruct /tmp/gw/runs/<run id>.jsonl --pubkey /tmp/gw/keys/seatbelt.pub
# run.start, model.request -> gpt-9, policy.check DENY, run.end
```

**Log.** [log/2026-09-28-v0.2.0.md](../log/2026-09-28-v0.2.0.md).

## 0.3.0: every major client, and recording with no gateway

2026-09-29.

**Planned.** The gateway design's second phase: OpenAI Responses, Gemini, config reload on `SIGHUP`, a central sink, OIDC for Claude Desktop, and a Compliance API importer.

**Shipped.** All six, plus local recording:
- The OpenAI Responses format (current Codex, the OpenAI Agents SDK) and the Gemini `generateContent` format (Gemini CLI, Google's Gen AI SDKs), with `seatbelt run codex` and `seatbelt run gemini`.
- Local recording. `seatbelt run claude|codex|gemini` works with no gateway to set up. The gateway starts inside the run on a free localhost port, and the CLI keeps its own sign-in. Each run is signed with a key made on first use. `seatbelt runs` lists runs by name, and `verify` and `reconstruct` take a name.
- Config reload, when the file changes or on `SIGHUP`. A withdrawn key's open sessions are ended and signed.
- OIDC sign-in for Claude Desktop. Users are identified by the provider's immutable id.
- A central sink that ships each signed ledger to S3-compatible object storage.
- `seatbelt import compliance`, which imports Claude Enterprise transcripts from Anthropic's Compliance API into signed ledgers. It is tested against a fake of the documented API only, not a live tenant.
- Supply chain: CodeQL, Hadolint, Atheris fuzzing in CI, Docker base images pinned by digest, and the gateway image on GHCR with signed provenance.

**Plan vs repo.**
- Local recording was not in the design's phasing. It records your own runs on your own machine. Recording for an organisation still goes through a gateway ([docs/local-recording.md](local-recording.md)).
- The importer's scope narrowed from "Team and Enterprise" to Enterprise, because the API serves transcript content to Claude Enterprise organizations only.
- The gateway's server packages became core dependencies, because `seatbelt run` starts the gateway to record locally.

**Read.** Designs for the [Responses format](plans/2026-09-28-responses-format.md), the [Gemini format](plans/2026-09-28-gemini-format.md) and the [Compliance importer](plans/2026-09-29-compliance-importer-design.md); [docs/local-recording.md](local-recording.md); [docs/deploy/compliance-import.md](deploy/compliance-import.md).

**Check.**

```sh
uv run pytest tests/unit/test_format_openai_responses.py tests/unit/test_format_gemini.py tests/unit/test_local.py tests/unit/test_compliance.py -q
uv run seatbelt run claude   # needs Claude Code installed; prints the run's name on exit; not run for this guide
uv run seatbelt runs         # lists the runs above; not run for this guide
uv run seatbelt reconstruct  # the latest local run; not run for this guide
```

**Log.** [log/2026-09-29-v0.3.0.md](../log/2026-09-29-v0.3.0.md).

## 0.4.0: one row per person, and erasure

2026-09-29.

**Planned.** Not in the guide. Both features answer something the importer design left open:
- **Who a person is** was deferred ("Decided 1"). One person could appear as an issued key, an identity provider's subject and an Anthropic user id.
- **Deletion** was documented with no tooling ("Decided 4"). A signed ledger keeps content that a user later deleted, which may conflict with an organisation's data-protection duties.

**Shipped.**
- `seatbelt report --people people.yaml`: one row per person across their principal ids. Ids are listed explicitly and never matched by e-mail, and an id listed for two people is refused. `report` also takes several folders.
- `seatbelt reconstruct` shows an imported answer's text, and marks imported messages `[unverified]`, `[marker]` or `[unavailable: <reason>]`.
- `seatbelt erase`, which removes whole ledgers recorded under a person's principal ids, inside a signed `erasure-…` record that holds only hashes, the `--case` reference, who ran it and counts. It is a dry run by default, and refuses while anything is writing. The importer never brings an erased conversation back. The sink's copies are listed, not deleted.
- `gateway serve` holds `<ledgers>/.lock` while it runs.

**Plan vs repo.** Whole ledgers, never events. Removing one person's events would break the hash chain and the manifest's file hash, and what remained would look the same as a tampered ledger. `erase` also does not decide whether a request must be honoured. The design leaves that to the organisation, citing GDPR Article 17's exceptions.

**Read.** [People and imported replay](plans/2026-09-29-people-and-imported-replay.md), the [erasure design](plans/2026-09-29-erasure-design.md), [docs/deploy/erasure.md](deploy/erasure.md).

**Check.** On the 0.2.0 gateway folder:

```sh
printf 'people:\n  Alice:\n    - alice@corp\n' > /tmp/gw/people.yaml
uv run seatbelt report /tmp/gw/runs --people /tmp/gw/people.yaml --pubkey /tmp/gw/keys/seatbelt.pub   # one row: Alice
uv run seatbelt erase /tmp/gw/runs --principal alice@corp --case DSR-1 --key /tmp/gw/keys/seatbelt.key
# lists the ledger; "Nothing changed. Run again with --yes to erase."
uv run seatbelt erase /tmp/gw/runs --principal alice@corp --case DSR-1 --key /tmp/gw/keys/seatbelt.key --yes
# leaves erasure-<time>-<id>.jsonl and its .attest.json
```

**Log.** [log/2026-09-29-v0.4.0.md](../log/2026-09-29-v0.4.0.md).

## 0.5.0: on PyPI

2026-09-29.

**Planned.** In the [PyPI design](plans/2026-09-29-pypi-design.md). The goal was `uv tool install seatbelt-ai`, then `seatbelt run claude`.

**Shipped.** Each release is published to PyPI as `seatbelt-ai` through Trusted Publishing, with no stored token and with PyPI's own attestations. The module and the command stay `seatbelt`. The provenance asset is renamed `seatbelt_ai-<version>.intoto.jsonl`.

**Plan vs repo.** The name `seatbelt` on PyPI belongs to an unrelated project, uploaded on 2026-04-13, which installs the same module and command. Only one `seatbelt` command can be installed at a time. The setup guide's first step was a PyPI name check.

**Read.** The [PyPI design](plans/2026-09-29-pypi-design.md).

**Check.**

```sh
uv build                                         # dist/seatbelt_ai-0.5.0-py3-none-any.whl and .tar.gz
uv tool install seatbelt-ai && seatbelt version   # installs from PyPI; not run for this guide
```

**Log.** [log/2026-09-29-v0.5.0.md](../log/2026-09-29-v0.5.0.md).

---

## Planned, not built

Items from the build guide and the learning pathway that have nothing in the repo at 0.5.0:

- An OpenTelemetry export (`export/otel.py`). Event attributes already use the OpenTelemetry GenAI names.
- NIST AI RMF and NCSC references on scenarios. They carry OWASP Agentic and MITRE ATLAS ids only.
- Importing AgentDojo and InjecAgent cases, and reproducing a published result.
- A LangGraph adapter.
- Sigstore keyless signing of runs. ADR 0002 calls it "the right next step for cross-organisation trust".
- A rendered HTML or PDF evidence report. ADR 0004 calls it "a later layer over the same pack".
- A `require_approval` policy effect with a named approver.
- An Inspect runner.

## Source map

Four sources are the maintainer's learning notes and are not in this repository: the build guide and the adapters milestone guide (both 2026-09-14), and the repo setup guide and the learning pathway (both 2026-09-10). Everything else is.

| Section | Claim | Source |
|---|---|---|
| Intro | build guide written 2026-09-14; 0.0.1 step by step, 0.0.2 to 0.1.0 guided; adapters guide gives 0.0.2's Anthropic adapter in full | build guide, header, intro and contents; adapters milestone guide, header; `CHANGELOG.md` 0.0.1 date |
| At a glance | release dates and headlines | `CHANGELOG.md` release headings; `ROADMAP.md` |
| Modules by release | files added and removed per tag; no `v0.0.1` tag | `git ls-tree -r --name-only <tag> -- src/seatbelt`; `git tag` |
| 0.0.1 planned | event model, ledger, redaction, Recorder, verify, timeline, CLI, 11 tests, six ADRs | build guide, "Milestone v0.0.1", steps 2 to 9 and "Decisions to record as ADRs" |
| 0.0.1 shipped | the list | `CHANGELOG.md` 0.0.1 |
| 0.0.1 vs repo | four of six rules in ADR 0001; OTel names and scope-chain kinds in docstrings | `docs/adr/0001-hash-chained-jsonl-ledger.md`; `src/seatbelt/ledger/events.py` (`Event`, `Kind`) |
| 0.0.1 vs repo | `schema_version`; unknown keys, 0600, fsync; `read_events` | `CHANGELOG.md` 0.0.2 Changed; 0.0.3 Security and Removed |
| 0.0.1 check | outputs `ok 11 events, chain intact, unattested` and `BROKEN at seq 9` | run 2026-09-29 |
| 0.0.2 planned | Adapter protocol, Anthropic adapter, primitives, fixture, example, OpenAI Agents processor, ADR-0008 | build guide, "Milestone v0.0.2"; adapters milestone guide, steps 1 to 8 |
| 0.0.2 shipped | the list, schema version 1, release workflow, INCOMPLETE, walkthrough example | `CHANGELOG.md` 0.0.2 |
| 0.0.2 vs repo | no ADR-0008; protocol and `attach` removed with reasons | `docs/adr/`; `CHANGELOG.md` 0.0.3 Removed |
| 0.0.2 vs repo | setup guide placed signed releases in week 15 | setup guide, step 16 |
| 0.0.3 planned | YAML rules, globs, effects, `require_approval`, ADR-0009 | build guide, "Milestone v0.0.3" |
| 0.0.3 shipped | policy engine, hardening, adapter call styles, `reconstruct` verifies first | `CHANGELOG.md` 0.0.3; `src/seatbelt/policy/engine.py` |
| 0.0.3 vs repo | rules are callables; fail closed in the docstring | `src/seatbelt/policy/engine.py` (`Check`, `Policy.evaluate`) |
| 0.0.3 vs repo | gateway `policy:` in YAML | `docs/deploy/gateway.md`, step 2; `CHANGELOG.md` 0.2.0 |
| 0.0.3 vs repo | adapters record no checks | `CHANGELOG.md` 0.0.3 Added |
| 0.0.4 planned | manifest fields, Sigstore, in-toto, `--signed`, Jinja2 report, ADR-0010 | build guide, "Milestone v0.0.4" |
| 0.0.4 shipped | Ed25519 sidecar, fields, commands, statuses | `CHANGELOG.md` 0.0.4; `src/seatbelt/attest/manifest.py` |
| 0.0.4 vs repo | Sigstore and HMAC rejected, and why; embedded key never trusted | `docs/adr/0002-signed-run-manifest.md` |
| 0.0.4 vs repo | SLSA provenance attestations from 0.0.2; `.intoto.jsonl` release asset from 0.3.0 | `CHANGELOG.md` 0.0.2 and 0.3.0 Added; `.github/workflows/release.yml` |
| 0.1.0 planned | OTel export, framework references, scenario pack, release engineering, launch and submissions | build guide, "Milestone v0.1.0" |
| 0.1.0 shipped | scenario pack, sandbox, evidence pack, overview, Best Practices evidence | `CHANGELOG.md` 0.1.0; ADRs 0003 to 0005 |
| 0.1.0 shipped | 8 scenarios: 7 with OWASP ids (3 also with ATLAS ids), 1 benign control with none; no other framework ids | `scenarios/*.yaml` |
| 0.1.0 vs repo | the Anthropic scenario target records through the adapter | `CHANGELOG.md` 0.2.0 Added |
| 0.1.0 vs repo | rendered output "a later layer over the same pack" | `docs/adr/0004-evidence-pack.md` |
| 0.1.0 vs repo | badge in the README from 0.4.0 | `CHANGELOG.md` 0.4.0 Added |
| 0.1.0 check | 7 PASS, 1 FAIL; `ok 8 runs, pack attested` | run 2026-09-29 |
| 0.2.0 | why a gateway | `docs/adr/0006-recording-gateway.md`, Context |
| 0.2.0 shipped | the list | `CHANGELOG.md` 0.2.0; ADRs 0006 and 0007 |
| 0.2.0 vs repo | `codex` preset planned, refused in 0.2.0; SSO rejected | `docs/plans/2026-09-18-gateway-design.md`, Decisions and Phasing; `CHANGELOG.md` 0.2.0; `docs/adr/0007-launcher.md` |
| 0.2.0 check | 403 `model gpt-9 is not allowed`; 204 on end; report and timeline | run 2026-09-29 |
| 0.3.0 planned | the six items | `docs/plans/2026-09-18-gateway-design.md`, Phasing |
| 0.3.0 shipped | the list | `CHANGELOG.md` 0.3.0 |
| 0.3.0 vs repo | local recording and the org path | `docs/local-recording.md` |
| 0.3.0 vs repo | Enterprise only | `docs/plans/2026-09-29-compliance-importer-design.md`, Scope |
| 0.3.0 vs repo | server packages became core | `CHANGELOG.md` 0.3.0 Changed |
| 0.4.0 | Decided 1 and 4 | `docs/plans/2026-09-29-compliance-importer-design.md`, Decided |
| 0.4.0 shipped | the list, including what the erasure record holds | `CHANGELOG.md` 0.4.0 |
| 0.4.0 vs repo | whole ledgers and why; GDPR Article 17 | `docs/plans/2026-09-29-erasure-design.md`, intro and "The constraint" |
| 0.4.0 check | one row for Alice; dry run; signed erasure record | run 2026-09-29 |
| 0.5.0 | goal, shipped, name clash, upload date | `docs/plans/2026-09-29-pypi-design.md`; `CHANGELOG.md` 0.5.0 |
| 0.5.0 vs repo | setup guide step 1 is a PyPI name check | setup guide, step 1 |
| 0.5.0 check | `seatbelt_ai-0.5.0` wheel and sdist | run 2026-09-29 |
| Planned, not built | OTel, NIST/NCSC, AgentDojo, InjecAgent, reproduction, LangGraph, Inspect | build guide and learning pathway; `grep -ri` over `src/`, `scenarios/`, `docs/` finds none built |
| Planned, not built | Sigstore and rendered report quotes | `docs/adr/0002-signed-run-manifest.md`; `docs/adr/0004-evidence-pack.md` |
| Planned, not built | `require_approval` | build guide, "Milestone v0.0.3"; not in `src/` |
