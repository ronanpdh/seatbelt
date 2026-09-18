"""Real Docker. Skipped when no daemon answers; CI runs it in its own job."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from seatbelt.ledger.store import read_events
from seatbelt.scenarios.sandbox import Docker, run_sandboxed

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
IMAGE = "seatbelt-target:test"


def _sh(*cmd: str) -> None:
    subprocess.run(cmd, check=True)  # noqa: S603  fixed argv, no shell


@pytest.fixture(scope="session")
def image() -> str:
    if shutil.which("docker") is None or Docker().cli("info", timeout=30).returncode != 0:
        if os.environ.get("CI"):
            pytest.fail("docker required in CI")
        pytest.skip("docker daemon not available")
    shutil.rmtree(ROOT / "dist", ignore_errors=True)  # one wheel, so COPY dist/*.whl is unambiguous
    _sh("uv", "build", "--out-dir", str(ROOT / "dist"))
    _sh("docker", "build", "-q", "-f", str(ROOT / "docker" / "Dockerfile"), "-t", IMAGE, str(ROOT))
    return IMAGE


def test_shipped_corpus_in_the_sandbox(tmp_path: Path, image: str) -> None:
    out = tmp_path / "runs"
    report = run_sandboxed(
        ROOT / "scenarios", "examples.scenario_target:target", image, out, target_dir=ROOT
    )
    assert [f.scenario_id for f in report.findings] == ["indirect-injection-refund"]
    events = list(read_events(out / "benign-order-status.jsonl"))
    assert "sha256:" in events[0].attrs["sandbox.image_digest"]  # repo@sha256:... or a bare id


NET_TARGET = """
import socket
from seatbelt.record.recorder import Recorder
from seatbelt.scenarios.model import Inputs

def target(rec: Recorder, inputs: Inputs) -> None:
    socket.create_connection(("1.1.1.1", 53), timeout=3).close()
    rec.outcome("network reachable", success=True)
"""
NET_SCENARIO = """\
id: {id}
title: network probe
owasp: []
severity: low
user_message: hi
egress: {egress}
checks:
  - run_ok: true
"""


@pytest.mark.parametrize(("egress", "expect_ok"), [("false", False), ("true", True)])
def test_egress_is_off_unless_declared(
    tmp_path: Path, image: str, egress: str, expect_ok: bool
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "net.yaml").write_text(NET_SCENARIO.format(id="net", egress=egress))
    tdir = tmp_path / "t"
    tdir.mkdir()
    (tdir / "probe.py").write_text(NET_TARGET)
    report = run_sandboxed(
        corpus, "probe:target", image, tmp_path / f"runs-{egress}", target_dir=tdir
    )
    assert report.results[0].run_ok is expect_ok
