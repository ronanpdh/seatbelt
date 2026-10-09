"""Each run's HTML page: what it shows, that ledger text cannot inject into it, that it loads
nothing, that only a ledger that verifies gets one, and how it is written."""

import json
import os
import re
import stat
import tempfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from seatbelt.attest.manifest import sidecar
from seatbelt.attest.sign import KEY_FILE, PUB_FILE, Signer, keygen
from seatbelt.cli import app
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger
from seatbelt.record.recorder import Recorder
from seatbelt.report import html, page_path
from seatbelt.report.html import DETAIL_LIMIT, PageError, build, render, write_page
from seatbelt.report.pack import build as build_pack

HOSTILE = (
    '<script>document.title="PWNED"</script><img src=x onerror=alert(1)>'
    '</details></pre><a href="javascript:alert(1)">x</a><style>body{display:none}</style>'
)


def _keys(tmp: Path) -> tuple[Signer, Path]:
    keys = tmp / "keys"
    if not (keys / KEY_FILE).exists():
        keygen(keys)
    return Signer.from_file(keys / KEY_FILE), keys / PUB_FILE


def _run(tmp: Path, text: str = "Refund order 1001", signed: bool = True) -> Path:
    """A run with two models, a tool, a refusal, a failed call and `text` in every place a
    ledger carries someone else's words."""
    signer = _keys(tmp)[0] if signed else None
    meta = {"principal.id": "alice", "run.name": "claude-1a2b", "client.user_agent": text}
    with Recorder.start(
        tmp / "runs", "seatbelt-gateway", "0.6.0", "alice-claude-1a2b-0001", meta, signer=signer
    ) as rec:
        rec.user_message("alice", text)
        with rec.model_call("claude-opus-5-5", {"messages": [text]}) as call:
            call.respond(
                {"content": [{"type": "text", "text": text}]},
                usage={
                    "input_tokens": 1500,
                    "output_tokens": 200,
                    "cache_read_input_tokens": 18000,
                    "cache_creation_input_tokens": 2400,
                },
                response_model="claude-opus-5-5-20260901",
            )
        with rec.tool_call("lookup_order", {"order": text}) as tool:
            tool.result({"status": text})
        denied = rec.tool_called("Bash", {"command": text})
        rec.policy_check("denylist", denied.id, False, f"Bash is denied: {text}")
        with rec.model_call("claude-haiku-5-5", {}) as call:
            call.respond({}, error=f"overloaded: {text}")
    return tmp / "runs" / "alice-claude-1a2b-0001.jsonl"


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, tuple[str, ...]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, tuple(name for name, _ in attrs)))


def _tags(page: str) -> list[tuple[str, tuple[str, ...]]]:
    parser = _Tags()
    parser.feed(page)
    return parser.tags


def test_the_page_shows_the_run_its_usage_and_what_was_checked(tmp_path: Path) -> None:
    ledger = _run(tmp_path)
    _, pub = _keys(tmp_path)
    page = build(ledger, pub)
    assert (page.title, page.principal, page.outcome) == ("claude-1a2b", "alice", "ok")
    assert page.complete and page.signature == "attested" and page.events == 11
    opus = page.models["claude-opus-5-5-20260901"]
    assert (opus.calls, opus.input_tokens, opus.cache_read_input_tokens) == (1, 1500, 18000)
    assert page.models["claude-haiku-5-5"].calls == 1  # a failed call is still a call
    assert page.total.output_tokens == 200
    assert page.tools == {"Bash": 1, "lookup_order": 1}
    assert [(r.rule, r.reason) for r in page.refusals] == [
        ("denylist", "Bash is denied: Refund order 1001")
    ]
    assert [f.error for f in page.failures] == ["overloaded: Refund order 1001"]
    assert page.final_hash == json.loads(ledger.read_text().splitlines()[-1])["hash"]
    assert f"--pubkey {pub}" in page.verify_command
    text = render(ledger, pub)
    assert "attested" in text and page.ledger_sha256 in text and "1,500" in text
    assert text.count('<li id="e') == 11  # one row per event
    assert "this page is a view, not evidence" in text.lower()


def test_ledger_text_is_escaped_everywhere_it_appears(tmp_path: Path) -> None:
    text = render(_run(tmp_path, HOSTILE))
    lowered = text.lower()
    assert "<script" not in lowered and "<img" not in lowered and "<style>body" not in lowered
    assert 'href="javascript' not in lowered  # the anchor's quote is escaped, so no href
    assert "&lt;script&gt;" in text and "&lt;img src=x onerror=alert(1)&gt;" in text
    # the injected closing tags did not close anything: the page's structure is the template's
    assert lowered.count("<details>") == lowered.count("</details>") == 11
    assert lowered.count("<pre>") == lowered.count("</pre>")


def test_bidi_overrides_and_control_characters_are_shown_not_obeyed(tmp_path: Path) -> None:
    text = render(_run(tmp_path, "pay ‮evil‬ now \x1b[31mred \x85"))
    assert "‮" not in text and "\x1b" not in text and "\x85" not in text
    assert "\\u202e" in text and "\\x85" in text


def test_the_page_loads_nothing_and_its_policy_comes_first(tmp_path: Path) -> None:
    text = render(_run(tmp_path, HOSTILE))
    head = text[text.index("<head>") + len("<head>") :].lstrip()
    assert head.startswith('<meta http-equiv="Content-Security-Policy"')
    policy = re.search(r'Content-Security-Policy" content="([^"]*)"', text)
    assert policy is not None and "default-src 'none'" in policy.group(1)
    assert "script-src" not in policy.group(1)  # no exception for scripts
    for tag, attrs in _tags(text):
        assert tag not in {"script", "link", "img", "iframe", "object", "embed", "base", "form"}
        assert "src" not in attrs and not any(a.startswith("on") for a in attrs)
    assert "@import" not in text and "url(" not in text
    assert set(re.findall(r'href="([^"]*)"', text)) <= {f"#e{n}" for n in range(11)}


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(st.text(max_size=200))
def test_no_ledger_text_changes_the_pages_markup(tmp_path: Path, text: str) -> None:
    """Whatever a prompt says, the page has the same elements and attributes as for a plain
    one: ledger text only ever lands as text. (Prefixed so that it is never empty: an empty
    client is left out of the page, which is a difference in content, not markup.)"""
    case = Path(tempfile.mkdtemp(dir=tmp_path))
    plain = render(_run(case / "plain", "x", signed=False))
    assert _tags(plain) == _tags(render(_run(case / "hostile", "x" + text, signed=False)))


def test_a_large_event_shows_its_start_and_end_and_says_what_is_left_out(
    tmp_path: Path,
) -> None:
    """A long request's newest turn is at its end, so the end is what must stay."""
    ledger = _run(tmp_path, "START" + "y" * (DETAIL_LIMIT + 5000) + "NEWEST")
    page = build(ledger)
    (user,) = [r for r in page.rows if r.kind == "user.message"]
    assert len(user.detail.encode()) + len(user.detail_end.encode()) <= DETAIL_LIMIT
    assert "START" in user.detail and "NEWEST" in user.detail_end
    whole = len(json.dumps(user_attrs(ledger), ensure_ascii=False).encode())  # compact
    assert user.cut == whole - DETAIL_LIMIT
    assert "bytes from the middle of this event are left out" in render(ledger)
    small = build(_run(tmp_path / "small", "short"))
    assert all(r.cut == 0 and r.detail_end == "" for r in small.rows)


def user_attrs(ledger: Path) -> object:
    (line,) = [x for x in ledger.read_text().splitlines() if '"user.message"' in x]
    return json.loads(line)["attrs"]


def test_a_broken_or_forged_ledger_gets_no_page(tmp_path: Path) -> None:
    ledger = _run(tmp_path)
    original = ledger.read_text()
    ledger.write_text(original.replace("Refund order 1001", "Refund order 9999", 1))
    with pytest.raises(PageError, match="chain broken"):
        build(ledger)
    ledger.write_text(original)
    side = json.loads(sidecar(ledger).read_text())
    side["events"] += 1
    sidecar(ledger).write_text(json.dumps(side))
    with pytest.raises(PageError, match="forged"):
        build(ledger)


def test_an_unsigned_ledger_is_shown_as_unsigned_and_refused_against_a_key(
    tmp_path: Path,
) -> None:
    ledger = _run(tmp_path, signed=False)
    assert build(ledger).signature == "unattested"
    _, pub = _keys(tmp_path)
    with pytest.raises(PageError, match="no signature"):
        build(ledger, pub)


def test_an_open_ledger_is_shown_as_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "open.jsonl"
    ledger = Ledger(path, "open")
    ledger.append(Kind.RUN_START, Actor(type=ActorType.AGENT, id="gw"), {"run.name": "r"})
    ledger.append(Kind.USER_MESSAGE, Actor(type=ActorType.USER, id="u"), {"text": "hi"})
    page = build(path)
    assert (page.outcome, page.complete, page.ended) == ("incomplete", False, "")
    assert "not ended" in render(path)


def test_a_failed_run_says_why(tmp_path: Path) -> None:
    with (
        pytest.raises(RuntimeError),
        Recorder.start(tmp_path, "agent", run_id="failing") as rec,
    ):
        rec.user_message("u", "hi")
        raise RuntimeError("the tool crashed")
    page = build(tmp_path / "failing.jsonl")
    assert page.outcome == "failed" and "the tool crashed" in page.outcome_reason


def test_the_page_is_written_whole_owner_only_and_replaced(tmp_path: Path) -> None:
    ledger = _run(tmp_path)
    page = write_page(ledger)
    assert page == page_path(ledger) == ledger.with_suffix(".html")
    assert stat.S_IMODE(page.stat().st_mode) == 0o600
    page.write_text("stale")
    os.chmod(page, 0o644)
    assert write_page(ledger) == page and page.read_text().startswith("<!doctype html>")
    assert stat.S_IMODE(page.stat().st_mode) == 0o600
    assert sorted(p.name for p in ledger.parent.iterdir() if p.name.startswith(".")) == []


def test_an_interrupted_write_leaves_no_partial_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _run(tmp_path)

    def crash(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(html.os, "replace", crash)
    with pytest.raises(OSError, match="disk full"):
        write_page(ledger)
    assert not page_path(ledger).exists()
    assert [p.name for p in ledger.parent.iterdir() if p.suffix in (".tmp", ".html")] == []


def test_reconstruct_html_writes_a_page_and_refuses_a_tampered_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEATBELT_HOME", str(tmp_path / "home"))  # not this machine's runs
    monkeypatch.chdir(tmp_path)
    ledger = _run(tmp_path)
    runner = CliRunner()
    out = runner.invoke(app, ["reconstruct", str(ledger), "--html"])
    assert out.exit_code == 0, out.output
    assert (tmp_path / "alice-claude-1a2b-0001.html").exists()
    out = runner.invoke(app, ["reconstruct", str(ledger), "--html", "--out", "r.html"])
    assert out.exit_code == 0 and (tmp_path / "r.html").exists()
    out = runner.invoke(app, ["reconstruct", str(ledger), "--html", "--out", "r.jsonl"])
    assert out.exit_code == 1 and "ends in .html" in out.output
    out = runner.invoke(app, ["reconstruct", str(ledger), "--out", "x.html"])
    assert out.exit_code == 1 and "add --html" in out.output
    ledger.write_text(ledger.read_text().replace("Refund", "Rebate", 1))
    out = runner.invoke(app, ["reconstruct", str(ledger), "--html", "--out", "t.html"])
    assert out.exit_code == 1 and not (tmp_path / "t.html").exists()


def test_pack_leaves_pages_out(tmp_path: Path) -> None:
    ledger = _run(tmp_path)
    write_page(ledger)
    build_pack(ledger.parent, tmp_path / "pack.zip")
    with zipfile.ZipFile(tmp_path / "pack.zip") as zf:
        assert not [n for n in zf.namelist() if n.endswith(".html")]


def test_the_template_ships_in_the_package() -> None:
    from importlib import resources

    assert resources.files("seatbelt.report").joinpath("templates", "run.html").is_file()
