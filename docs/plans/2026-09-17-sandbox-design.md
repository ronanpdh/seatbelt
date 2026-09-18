# Docker sandbox design (0.1.0, part 3)

Date: 2026-09-17. Status: agreed, not yet implemented.

## Problem

`seatbelt scenarios` runs the target in-process. A scenario that succeeds in making the agent run a shell command or reach the network does so on the host. STANDARDS says targets are sandboxed by default with no network unless a scenario declares egress, and that a run records the sandbox image digest.

## Decisions

- **Same callable, run inside a container.** The `target(rec, inputs)` contract is unchanged. One container per scenario, started by the host runner with the user's code mounted read-only at `/target` and an output directory at `/out`. A small in-container entry point (`seatbelt.scenarios.child`) records that one scenario exactly as the in-process runner does. The host reads the ledger, evaluates checks, signs with `--key` if given, and writes `findings.json`. The signing key never enters the container.
- **User-supplied image**, `--image`, built `FROM` the shipped `docker/Dockerfile` (python:3.12-slim plus the seatbelt wheel). The runner refuses an image whose seatbelt version differs from the host's: ledger schema and child protocol are one package.
- **Egress declared per scenario.** `egress: false` is the default and runs `--network none`; `true` uses Docker's default bridge. Host allowlists are out of scope.
- **Hardening flags on every container**: `--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 --memory 1g --user <host uid:gid>`, plus `PYTHONDONTWRITEBYTECODE=1`. Named containers so a timeout can `docker kill` the container, not just the CLI client.
- **Docker via the `docker` CLI** as an argument list through `subprocess`, never a shell. No new Python dependency.
- **Failures kept apart.** Harness failure (no docker, no image, version mismatch, missing target dir) is a `SandboxError` before any container starts. A container that exits without writing a ledger is a `SandboxError` naming the scenario and the stderr file. A target crash or timeout with a ledger present is a result: the ledger is evaluated as it is, and a timeout after `run.start` shows as INCOMPLETE so `run_ok: true` fails. Nothing is fabricated.
- Container stderr is written to `<out>/<id>.stderr.txt` as a debugging aid. It is not part of the ledger and the evidence pack does not include it.

Rejected: a JSON protocol over stdin/stdout (a second protocol to version, and the Recorder no longer runs beside the agent), one container for the whole corpus (no per-scenario network policy, a compromised target taints later scenarios), auto-building the user's Dockerfile, per-host egress via a proxy sidecar.

## Module

- `src/seatbelt/scenarios/runner.py`: `run()` split into `refuse_taken`, `record` (one in-process scenario into a directory, shared with the child) and `collect` (read, evaluate, summarise, write `findings.json`, shared with the sandbox).
- `src/seatbelt/scenarios/child.py`: `ChildSpec` (scenario, target spec, metadata) read from the `SEATBELT_CHILD` environment variable; `main(target_dir, out_dir, env)` imports the target from `/target` and calls `record`. Testable in-process.
- `src/seatbelt/scenarios/sandbox.py`: `Docker` (cli wrapper: `check`, `image_digest`, `image_version`, `kill`), `run_args(scenario, image, target_dir, out, spec_json, name)`, `run_sandboxed(corpus, target, image, out, *, target_dir, timeout, signer, docker) -> Report`.
- `Scenario` gains `egress: bool = False`; `docs/schema/scenario.json` regenerated.
- `docker/Dockerfile`: base image built from `dist/*.whl`.

## CLI

`seatbelt scenarios <corpus> --target m:f --image <img> [--target-dir .] [--timeout 120] [--out runs] [--key]`. `--image` selects the sandbox; without it the in-process path is unchanged. `--list` gains an `egress` column.

## Tests

- Unit (`tests/unit/test_sandbox.py`), no daemon: a fake `docker` script on `PATH` that answers `info`, `image inspect`, the version probe and `kill`, and for the child command parses `-v` and `-e` and runs `child.main` in-process. Covers `run_args` for both egress values, the child in isolation, the full sandboxed corpus with the demo target (7 pass, 1 fail, image digest in `run.start`, stderr files, sidecars with a signer), version mismatch, missing image, docker absent, timeout (`kill` issued, `SandboxError` naming the stderr file), CLI with `--image`, `--list` egress column.
- Integration (`tests/integration/test_sandbox_docker.py`, marker `integration`, skipped without a daemon): build the image from the checkout, run the shipped corpus, expect 7 pass and 1 fail with a real digest; a target that opens a socket fails under `egress: false` and succeeds under `egress: true`. CI runs it in a separate job.

## Docs

ADR 0005, `docker/Dockerfile`, README "Sandbox it", CHANGELOG, ROADMAP item Done, CI job.

## Out of scope

Per-host egress allowlists, non-Python targets, Podman, GPU, resource tuning, a registry-published image.
