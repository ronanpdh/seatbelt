"""Run scenario targets in Docker, one container per scenario, no network unless declared."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from uuid import uuid4

from seatbelt import __version__
from seatbelt.attest.sign import Signer, attest
from seatbelt.scenarios.child import ENV, OUT_DIR, TARGET_DIR, ChildSpec
from seatbelt.scenarios.model import Scenario, corpus_sha256, load_corpus
from seatbelt.scenarios.runner import Report, collect, refuse_taken

HARDENING = [
    "--read-only",
    "--tmpfs",
    "/tmp",  # noqa: S108  the container's own tmpfs, not a host path
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--pids-limit",
    "256",
    "--memory",
    "1g",
    "--memory-swap",
    "1g",
]
_VERSION_PROBE = "import importlib.metadata as m; print(m.version('seatbelt'))"
_PROBE_TIMEOUT = 30


class SandboxError(Exception):
    """The sandbox cannot run. The message names the cause and, if any, the stderr file."""


class SandboxTimeout(SandboxError):
    """A docker command exceeded its timeout; the client was killed, the container may not be."""


def _user() -> str:
    return f"{os.getuid()}:{os.getgid()}"


@dataclass(frozen=True)
class Docker:
    binary: str = "docker"

    def cli(
        self, *args: str, timeout: float | None = None, output: IO[str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run one docker command; `output` streams stdout and stderr to a file instead."""
        try:
            # arguments are a list, never a shell string
            return subprocess.run(  # noqa: S603
                [self.binary, *args],
                text=True,
                timeout=timeout,
                check=False,
                stdout=subprocess.PIPE if output is None else output,
                stderr=subprocess.PIPE if output is None else subprocess.STDOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxTimeout(f"docker {args[0]} timeout after {timeout}s") from exc
        except OSError as exc:  # binary vanished, argv too long
            raise SandboxError(f"docker {args[0]}: {exc}") from exc

    def check(self) -> None:
        if shutil.which(self.binary) is None:
            raise SandboxError(f"{self.binary} not found on PATH")
        probe = self.cli("info", "--format", "{{.ServerVersion}}", timeout=_PROBE_TIMEOUT)
        if probe.returncode != 0:
            raise SandboxError(f"docker daemon unavailable: {probe.stderr.strip()}")

    def image_digest(self, image: str) -> str:
        fmt = "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}"
        probe = self.cli("image", "inspect", "--format", fmt, image, timeout=_PROBE_TIMEOUT)
        if probe.returncode != 0:
            raise SandboxError(f"image {image!r} not found: {probe.stderr.strip()}")
        return probe.stdout.strip()

    def image_version(self, image: str) -> str:
        probe = self.cli(
            "run",
            "--rm",
            "--network",
            "none",
            *HARDENING,
            "--user",
            _user(),
            image,
            "python",
            "-c",
            _VERSION_PROBE,
            timeout=_PROBE_TIMEOUT,
        )
        if probe.returncode != 0:
            raise SandboxError(f"image {image!r} cannot import seatbelt: {probe.stderr.strip()}")
        return probe.stdout.strip()

    def kill(self, name: str) -> None:
        self.cli("kill", name, timeout=_PROBE_TIMEOUT)


def run_args(
    scenario: Scenario, image: str, target_dir: Path, out: Path, spec_json: str, name: str
) -> list[str]:
    network = "bridge" if scenario.egress else "none"
    return [
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        network,
        *HARDENING,
        "--user",
        _user(),
        "-e",
        f"{ENV}={spec_json}",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-v",
        f"{target_dir.resolve()}:{TARGET_DIR}:ro",
        "-v",
        f"{out.resolve()}:{OUT_DIR}",
        image,
        "python",
        "-m",
        "seatbelt.scenarios.child",
    ]


def run_sandboxed(
    corpus: Path,
    target: str,
    image: str,
    out: Path,
    *,
    target_dir: Path,
    timeout: float = 120,
    signer: Signer | None = None,
    docker: Docker | None = None,
) -> Report:
    docker = docker or Docker()
    if os.getuid() == 0:
        raise SandboxError(
            "refusing to run the sandbox as root; run seatbelt as an unprivileged user"
        )
    scenarios = load_corpus(corpus)
    refuse_taken(scenarios, out, signer)
    if not target_dir.is_dir():
        raise SandboxError(f"target directory {target_dir} does not exist")
    docker.check()
    image_digest = docker.image_digest(image)
    version = docker.image_version(image)
    if version != __version__:
        raise SandboxError(f"image {image!r} has seatbelt {version}, this host has {__version__}")
    digest = corpus_sha256(corpus)
    out.mkdir(parents=True, exist_ok=True)

    def one(s: Scenario) -> Path:
        meta = {
            "corpus.sha256": digest,
            "sandbox.image": image,
            "sandbox.image_digest": image_digest,
            "sandbox.egress": s.egress,
        }
        spec = ChildSpec(scenario=s, target=target, metadata=meta).model_dump_json()
        name = f"seatbelt-{s.id}-{uuid4().hex[:8]}"
        path = out / f"{s.id}.jsonl"
        stderr_file = out / f"{s.id}.stderr.txt"
        # a private /out per container, so one scenario cannot touch another's ledger
        scratch = Path(tempfile.mkdtemp(prefix=f".{s.id}-", dir=out))
        try:
            with stderr_file.open("w", encoding="utf-8") as fh:
                try:
                    docker.cli(
                        *run_args(s, image, target_dir, scratch, spec, name),
                        timeout=timeout,
                        output=fh,
                    )
                except SandboxTimeout:
                    docker.kill(name)
                    fh.write(f"\n[seatbelt] timeout after {timeout}s, container killed\n")
            produced = scratch / path.name
            if not produced.exists():
                raise SandboxError(f"{s.id}: container exited without a ledger; see {stderr_file}")
            shutil.move(produced, path)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if signer is not None:
            attest(path, signer)
        return path

    return collect(scenarios, digest, out, one)
