"""The gateway refuses a request shape its format cannot record, and forwards nothing."""

from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.testclient import TestClient

from seatbelt.gateway.app import create_app
from seatbelt.gateway.config import add_principal, load_config
from seatbelt.gateway.sessions import Sessions


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/v1beta/models/gemini-2.5-pro:generateContent",
            {"contents": [], "system_instruction": {"parts": [{"text": "x"}]}},
        ),
        (
            "/v1beta/models/gemini-2.5-pro:generateContent",
            {"contents": [], "generationConfig": {"candidateCount": 2}},
        ),
        ("/v1/chat/completions", {"model": "gpt-5", "messages": [], "functions": []}),
        ("/v1/chat/completions", {"model": "gpt-5", "messages": [], "n": 2}),
        ("/v1/responses", {"model": "gpt-5", "input": "hi", "background": True}),
    ],
)
def test_unrecordable_requests_are_refused_and_not_forwarded(
    tmp_path: Path, path: str, body: dict[str, Any]
) -> None:
    cfg_path = tmp_path / "gateway.yaml"
    cfg_path.write_text(
        "ledgers: runs\nupstreams:\n"
        "  openai: {url: https://api.openai.com, key_env: K}\n"
        "  gemini: {url: https://generativelanguage.googleapis.com, key_env: K}\n"
    )
    key = add_principal(cfg_path, "alice@corp")
    cfg = load_config(cfg_path)
    forwarded: list[httpx2.Request] = []

    def upstream(request: httpx2.Request) -> httpx2.Response:
        forwarded.append(request)
        return httpx2.Response(200, json={})

    sessions = Sessions(cfg.ledgers, None, idle=60)
    app = create_app(cfg, sessions, httpx2.MockTransport(upstream))
    with TestClient(app) as client:
        resp = client.post(path, json=body, headers={"authorization": f"Bearer {key}"})
    assert resp.status_code == 400, resp.text
    assert forwarded == []


def test_a_body_nested_past_what_the_ledger_can_hold_is_refused(tmp_path: Path) -> None:
    cfg_path = tmp_path / "gateway.yaml"
    cfg_path.write_text(
        "ledgers: runs\nupstreams:\n  openai: {url: https://api.openai.com, key_env: K}\n"
    )
    key = add_principal(cfg_path, "alice@corp")
    cfg = load_config(cfg_path)
    forwarded: list[httpx2.Request] = []

    def upstream(request: httpx2.Request) -> httpx2.Response:
        forwarded.append(request)
        return httpx2.Response(200, json={})

    deep: Any = "x"
    for _ in range(300):
        deep = [deep]
    app = create_app(cfg, Sessions(cfg.ledgers, None, idle=60), httpx2.MockTransport(upstream))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-5", "messages": [{"role": "user", "content": deep}]},
            headers={"authorization": f"Bearer {key}"},
        )
    assert resp.status_code == 400 and "nested too deeply" in resp.text
    assert forwarded == []
