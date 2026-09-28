import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import botocore.auth
import httpx2
import pytest
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from starlette.applications import Starlette
from starlette.testclient import TestClient

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import Signer, keygen
from seatbelt.gateway import serve as serve_mod
from seatbelt.gateway.config import add_principal, load_config
from seatbelt.gateway.sessions import Sessions
from seatbelt.gateway.sink import S3Store, Sink, StoreError, sigv4_headers
from seatbelt.record.recorder import Recorder

NOW = datetime(2026, 9, 28, 12, 34, 56, tzinfo=UTC)
AK, SK = "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"  # fake test credentials


def test_signing_matches_botocore(monkeypatch: pytest.MonkeyPatch) -> None:
    """botocore's SigV4 signer is the oracle: same request, same time, same signature."""
    url = "https://ledgers.fsn1.your-objectstorage.com/runs/alice_corp-x-1a2b3c4d.jsonl"
    body = b'{"seq":0}\n'
    ours = sigv4_headers(
        "PUT", url, body, {"content-type": "application/x-ndjson"}, "fsn1", AK, SK, NOW
    )
    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: NOW.replace(tzinfo=None))
    request = AWSRequest(
        method="PUT",
        url=url,
        data=body,
        headers={
            "content-type": "application/x-ndjson",
            "x-amz-content-sha256": hashlib.sha256(body).hexdigest(),
        },
    )
    botocore.auth.SigV4Auth(Credentials(AK, SK), "s3", "fsn1").add_auth(request)
    assert ours["Authorization"] == request.headers["Authorization"]
    assert ours["x-amz-date"] == request.headers["X-Amz-Date"] == "20260928T123456Z"


def _store(handler: httpx2.MockTransport) -> S3Store:
    return S3Store(
        "https://fsn1.your-objectstorage.com", "ledgers", "fsn1", AK, SK, handler, lambda: NOW
    )


def test_s3_store_puts_to_the_bucket_host_and_reports_failures() -> None:
    seen: list[httpx2.Request] = []

    def ok(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200)

    _store(httpx2.MockTransport(ok)).put("runs/a b.jsonl", b"x", "application/x-ndjson")
    (sent,) = seen
    assert str(sent.url) == "https://ledgers.fsn1.your-objectstorage.com/runs/a%20b.jsonl"
    assert sent.method == "PUT" and sent.content == b"x"
    assert sent.headers["x-amz-content-sha256"] == hashlib.sha256(b"x").hexdigest()
    assert sent.headers["authorization"].startswith(
        f"AWS4-HMAC-SHA256 Credential={AK}/20260928/fsn1/s3/"
    )
    assert not any(h.startswith("x-amz-checksum") for h in sent.headers)  # none; see the module
    for status in (403, 503):
        refused = _store(httpx2.MockTransport(lambda _, s=status: httpx2.Response(s, text="no")))
        with pytest.raises(StoreError, match=str(status)):
            refused.put("k", b"x", "text/plain")

    def down(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    with pytest.raises(StoreError, match="ConnectError"):
        _store(httpx2.MockTransport(down)).put("k", b"x", "text/plain")


class FakeStore:
    def __init__(self, failures: int = 0) -> None:
        self.objects: dict[str, bytes] = {}
        self.failures = failures
        self.done = threading.Event()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        if self.failures:
            self.failures -= 1
            raise StoreError("503: slow down")
        self.objects[key] = data
        if key.endswith(".attest.json"):
            self.done.set()


def _closed_ledger(root: Path, run_id: str, signer: Signer | None) -> Path:
    with Recorder.start(root, agent_id="gateway", run_id=run_id, signer=signer) as rec:
        rec.user_message("u", "hi")
    return root / f"{run_id}.jsonl"


def test_a_closed_ledger_and_its_signature_ship_once(tmp_path: Path) -> None:
    path = _closed_ledger(tmp_path, "r1", Signer.generate())
    store = FakeStore(failures=2)  # the store refuses twice, then takes it
    sink = Sink(store, tmp_path, prefix="runs/", backoff=(0.01,))
    sink.start()
    sink.ship(path)
    assert store.done.wait(5)
    assert sink.stop(timeout=5) == 0
    assert store.objects == {
        "runs/r1.jsonl": path.read_bytes(),
        "runs/r1.attest.json": sidecar(path).read_bytes(),
    }
    mark = json.loads((tmp_path / ".shipped" / "r1").read_text())
    assert mark["runs/r1.jsonl"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert sink.shipped(path) and sink.catch_up() == 0  # never again


def test_catch_up_queues_closed_unshipped_ledgers_only(tmp_path: Path) -> None:
    done = _closed_ledger(tmp_path, "done", None)
    _closed_ledger(tmp_path, "shipped", None)
    Recorder.start(tmp_path, agent_id="gateway", run_id="open").__enter__().user_message("u", "x")
    (tmp_path / "broken.jsonl").write_text("not json\n")
    sink = Sink(FakeStore(), tmp_path)
    (tmp_path / ".shipped" / "shipped").write_text("{}")
    assert sink.catch_up() == 1  # not the shipped one, the open one or the broken one
    store = FakeStore()
    sink = Sink(store, tmp_path)
    sink.catch_up()
    sink.start()
    assert sink.stop(timeout=5) == 0
    assert set(store.objects) == {"done.jsonl"}  # no signing key: no sidecar
    assert store.objects["done.jsonl"] == done.read_bytes()


def test_stop_reports_what_is_left_for_the_next_start(tmp_path: Path) -> None:
    for n in range(3):
        _closed_ledger(tmp_path, f"r{n}", None)
    sink = Sink(FakeStore(failures=10**6), tmp_path, backoff=(60.0,))  # the store is down
    sink.catch_up()
    sink.start()
    assert sink.stop(timeout=0.2) == 3
    assert list((tmp_path / ".shipped").iterdir()) == []


def test_sessions_hand_each_ledger_over_once_it_is_signed(tmp_path: Path) -> None:
    handed: list[tuple[Path, bool]] = []
    sessions = Sessions(
        tmp_path,
        Signer.generate(),
        idle=60,
        on_close=lambda p: handed.append((p, sidecar(p).exists())),
    )
    s = sessions.get("alice@corp", None, {})
    sessions.release(s)
    sessions.close_all()
    assert handed == [(s.rec.ledger.path, True)]


def test_serve_ships_sessions_and_catches_up_on_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keygen(tmp_path / "keys")
    path = tmp_path / "gateway.yaml"
    path.write_text(
        "signing_key: keys/seatbelt.key\nledgers: runs\n"
        "upstreams:\n  anthropic: {url: https://api.anthropic.com, key_env: K}\n"
        "sink: {url: https://fsn1.your-objectstorage.com, bucket: b, region: fsn1}\n"
    )
    key = add_principal(path, "alice@corp")
    earlier = _closed_ledger(load_config(path).ledgers, "earlier", None)
    store = FakeStore()

    def fake_store(*_: object, **__: object) -> FakeStore:
        return store

    monkeypatch.setattr(serve_mod, "S3Store", fake_store)
    monkeypatch.setenv("SEATBELT_SINK_ACCESS_KEY", AK)
    monkeypatch.setenv("SEATBELT_SINK_SECRET_KEY", SK)

    def fake_run(app: Starlette, **_: object) -> None:
        app.state.transport = httpx2.MockTransport(lambda _: httpx2.Response(200, json={}))
        with TestClient(app) as client:
            client.post("/v1/messages", json={"model": "m"}, headers={"x-api-key": key})

    monkeypatch.setattr(serve_mod.uvicorn, "run", fake_run)
    serve_mod.serve(path)
    session = [k for k in store.objects if k.startswith("alice_corp")]
    assert len(session) == 2  # the ledger and its signature, shipped at shutdown
    assert store.objects["earlier.jsonl"] == earlier.read_bytes()  # left from before


def test_a_sink_without_its_keys_in_the_environment_refuses_to_start(tmp_path: Path) -> None:
    path = tmp_path / "gateway.yaml"
    path.write_text(
        "ledgers: runs\nupstreams: {}\n"
        "sink: {url: https://fsn1.your-objectstorage.com, bucket: b, region: fsn1}\n"
    )
    with pytest.raises(ValueError, match="SEATBELT_SINK_ACCESS_KEY, SEATBELT_SINK_SECRET_KEY"):
        serve_mod.make_sink(load_config(path), {})
