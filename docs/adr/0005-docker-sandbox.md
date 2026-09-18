# 5. Docker sandbox: one hardened container per scenario

- Status: accepted
- Date: 2026-09-17

## Context

`seatbelt scenarios` fed adversarial input to a target running in the harness's own process. An attack that persuades the agent to run a command or reach the network does so on the host. STANDARDS asks for sandboxed targets with no network unless a scenario declares egress, and for the image digest to be pinned in the run.

## Decision

The `target(rec, inputs)` contract is unchanged. With `--image`, the host starts one container per scenario from a user-supplied image built on `docker/Dockerfile`, mounts the user's code read-only at `/target` and an output directory at `/out`, and runs `seatbelt.scenarios.child`, which records that one scenario with the same `Recorder` the in-process path uses. The host then evaluates checks, signs if asked, and writes `findings.json`. The key never enters the container.

Every container runs `--network none` unless the scenario says `egress: true` (Docker's default bridge), and always `--read-only`, `--tmpfs /tmp`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit 256`, `--memory 1g` with `--memory-swap 1g`, as the host's uid and gid so `/out` is writable without root. Containers are named so a timeout kills the container, not only the CLI client. The runner refuses to start as root, since uid 0 inside the container would own every file it writes. The runner refuses an image whose seatbelt version differs from the host's.

Rejected: a JSON protocol across the boundary (a second protocol to version, and the Recorder would no longer run beside the agent), one container for the whole corpus (no per-scenario network policy, and a compromised target taints later scenarios), auto-building the user's Dockerfile, per-host egress through a proxy sidecar.

## Consequences

- A successful attack is contained and recorded, not executed on the host.
- `run.start` carries `sandbox.image`, `sandbox.image_digest` and `sandbox.egress`, so a reviewer knows what environment produced the ledger and whether it could reach the network.
- Harness failures (no daemon, no image, version mismatch) stop before any container starts. A container that writes no ledger is an error naming its stderr file. A crash or timeout with a ledger is a result, evaluated as-is; nothing is fabricated.
- Each container gets a private scratch directory as `/out`; the host moves the one expected ledger out of it afterwards, so one scenario cannot write or overwrite another's ledger.
- Everything under `--target-dir` is visible to the target, so keys, `.env` files and earlier runs belong outside it. The CLI refuses a `--key` inside `--target-dir`.
- Container output lands in `<out>/<id>.stderr.txt` for debugging; it is not part of the ledger or the evidence pack.
- Unit tests run against a fake `docker` on `PATH`; real-daemon tests are a separate CI job.
