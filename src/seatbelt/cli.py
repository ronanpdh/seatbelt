import contextlib
import importlib
import os
import sys
from pathlib import Path
from typing import Annotated, cast

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Column, Table
from rich.text import Text

from seatbelt import __version__
from seatbelt.attest.manifest import AttestError, sidecar
from seatbelt.attest.sign import PUB_FILE, Signer
from seatbelt.attest.sign import attest as sign_ledger
from seatbelt.attest.sign import keygen as make_keys
from seatbelt.gateway.config import add_principal
from seatbelt.gateway.launcher import data_dir, load_client_config, run_cli
from seatbelt.ledger.events import Kind
from seatbelt.ledger.store import LedgerError, read_events
from seatbelt.record.recorder import Recorder
from seatbelt.report.fleet import People
from seatbelt.report.fleet import fleet as build_fleet
from seatbelt.report.pack import PackError, PackStatus
from seatbelt.report.pack import build as build_pack
from seatbelt.report.pack import verify_pack as check_pack
from seatbelt.report.timeline import timeline
from seatbelt.scenarios.model import OWASP_AGENTIC, ScenarioError, load_corpus
from seatbelt.scenarios.runner import Target
from seatbelt.scenarios.runner import run as run_corpus
from seatbelt.scenarios.sandbox import SandboxError, run_sandboxed
from seatbelt.terminal import printable
from seatbelt.verify.attest import Attestation, AttestVerdict, verify_attestation
from seatbelt.verify.chain import Verdict, verify_file

app = typer.Typer(help="Attributable, reconstructable, provable records of agent interactions.")
console = Console(soft_wrap=True)  # never split a path or reason across lines


@app.callback()
def main() -> None:
    """Agent audit harness."""


@app.command()
def version() -> None:
    """Print the harness version."""
    console.print(__version__)


PubKey = Annotated[Path | None, typer.Option(help="public key from keygen; checks the attestation")]
LedgerRef = Annotated[
    str | None,
    typer.Argument(
        help="a ledger file, or a run's name or id from `seatbelt runs` "
        "(e.g. claude-99ce72ff); default: your latest run",
        show_default=False,
    ),
]


def _local_runs(pubkey: Path | None) -> tuple[Path, Path | None]:
    cfg = load_client_config()
    root = data_dir()
    local_pub = root / "keys" / PUB_FILE
    return cfg.ledgers or root / "runs", pubkey or (local_pub if local_pub.exists() else None)


def _run_name(ledger: Path) -> str | None:
    try:
        first = next(read_events(ledger), None)
    except (OSError, UnicodeDecodeError, LedgerError):
        return None
    name = first.attrs.get("run.name") if first is not None else None
    return name if isinstance(name, str) else None


def _resolve(ref: str | None, pubkey: Path | None) -> tuple[Path, Path | None]:
    """The ledger `ref` names, and the key to check it with. A path is used as given; else
    `ref` is looked up in this machine's runs: a file name, a run id (the file name without
    .jsonl), or a run's name as `seatbelt run` prints it. None: the latest run. A local run is
    checked against this machine's key unless `pubkey` is given."""
    if ref is not None and (Path(ref).expanduser().is_file() or os.sep in ref or "/" in ref):
        ledger = Path(ref).expanduser()  # a path: checked as given, missing or not
        runs, local_pub = _local_runs(pubkey)
        mine = ledger.resolve().parent == runs.resolve()
        return ledger, pubkey or (local_pub if mine else None)
    runs, local_pub = _local_runs(pubkey)
    ledgers = sorted(runs.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if ref is None:
        found = ledgers[:1]
        if not found:
            raise ValueError(f"no runs recorded yet in {runs}. Start one with: seatbelt run claude")
    else:
        name = ref.removesuffix(".jsonl")
        found = [p for p in ledgers if p.stem == name] or [
            p for p in ledgers if f"-{name}-" in p.stem and _run_name(p) == name
        ]
        if not found:
            raise ValueError(
                f"no ledger or run {ref!r}: not a file, and not in {runs}. "
                "List your runs with: seatbelt runs"
            )
        if len(found) > 1:
            names = ", ".join(p.stem for p in found)
            raise ValueError(f"{ref!r} names {len(found)} ledgers ({names}); give one of these")
    return found[0], local_pub


def _resolved(ref: str | None, pubkey: Path | None) -> tuple[Path, Path | None]:
    try:
        return _resolve(ref, pubkey)
    except ValueError as exc:  # also a bad client config
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc


def _shown(text: str) -> str:
    """Untrusted text (run ids, member names, reasons that quote them) made safe to print."""
    return escape(printable(text))


def _check(ledger: Path, pubkey: Path | None = None) -> tuple[Verdict, AttestVerdict]:
    """Exit 1 on a broken chain, a forged attestation, or none when a key was given."""
    verdict = verify_file(ledger)
    if not verdict.ok:
        where = "" if verdict.first_bad_seq is None else f" at seq {verdict.first_bad_seq}"
        console.print(f"[red]BROKEN[/]{where}: {_shown(verdict.reason or '')}")
        raise typer.Exit(code=1)
    try:
        att = verify_attestation(ledger, pubkey)
    except AttestError as exc:
        console.print(f"[red]{_shown(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    if att.status is Attestation.FORGED:
        console.print(f"[red]FORGED[/]: {_shown(att.reason or '')}")
        raise typer.Exit(code=1)
    if att.status is Attestation.UNATTESTED and pubkey is not None:
        console.print(f"[red]UNATTESTED[/]: no {_shown(str(sidecar(ledger)))}")
        raise typer.Exit(code=1)
    if att.status is Attestation.UNCHECKED:
        console.print(f"[yellow]UNCHECKED[/] {_shown(att.reason or '')}")
    if not verdict.complete:
        console.print(
            f"[yellow]INCOMPLETE[/] {verdict.events} events, chain intact but no matching "
            "run.end: truncated or still running"
        )
    return verdict, att


@app.command()
def verify(ledger: LedgerRef = None, pubkey: PubKey = None) -> None:
    """Check a run ledger's hash chain and attestation. Exit 1 if altered, forged or incomplete."""
    path, pubkey = _resolved(ledger, pubkey)
    verdict, att = _check(path, pubkey)
    if not verdict.complete:
        raise typer.Exit(code=1)
    console.print(f"[green]ok[/] {verdict.events} events, chain intact, {att.status}")


@app.command()
def reconstruct(ledger: LedgerRef = None, pubkey: PubKey = None) -> None:
    """Print the run as a timeline a reviewer can read. Refuses an altered ledger."""
    path, pubkey = _resolved(ledger, pubkey)
    _check(path, pubkey)
    timeline(path, console)


@app.command()
def runs(
    limit: Annotated[int, typer.Option(help="how many to show, newest first")] = 20,
) -> None:
    """List the runs `seatbelt run` recorded on this machine, newest first, by the name
    `seatbelt reconstruct` and `seatbelt verify` take."""
    try:
        folder, _ = _local_runs(None)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    ledgers = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not ledgers:
        console.print(f"No runs recorded yet in {folder}. Start one with: seatbelt run claude")
        return
    table = Table(Column("run", no_wrap=True), "started", "model calls", "status")
    for path in ledgers[:limit]:
        try:
            events = list(read_events(path))
        except (OSError, UnicodeDecodeError, LedgerError):
            table.add_row(Text(path.stem), "", "", "[red]unreadable[/]")
            continue
        first = events[0] if events else None
        name = first.attrs.get("run.name") if first is not None else None
        started = first.ts.astimezone().strftime("%Y-%m-%d %H:%M") if first is not None else ""
        calls = sum(e.kind is Kind.MODEL_REQUEST for e in events)
        ended = bool(events) and events[-1].kind is Kind.RUN_END
        table.add_row(
            Text(name if isinstance(name, str) else path.stem),
            started,
            str(calls),
            "ended" if ended else "[yellow]open[/]",
        )
    console.print(table)
    console.print(f"{len(ledgers)} runs in {escape(str(folder))}")
    console.print("Replay one with: seatbelt reconstruct <run>")


@app.command()
def keygen(directory: Annotated[Path, typer.Argument()] = Path(".")) -> None:
    """Write an Ed25519 signing key (seatbelt.key, mode 0600) and its public key (seatbelt.pub)."""
    try:
        key, pub = make_keys(directory)
    except AttestError as exc:
        console.print(f"[red]{_shown(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"wrote {_shown(str(key))} and {_shown(str(pub))}")


@app.command()
def attest(
    ledger: Path, key: Annotated[Path, typer.Option(help="private key from keygen")]
) -> None:
    """Sign a finished ledger into <run id>.attest.json. Refuses a broken or incomplete chain."""
    if sidecar(ledger).exists():
        console.print(f"[red]{_shown(str(sidecar(ledger)))} exists; delete it to re-sign[/]")
        raise typer.Exit(code=1)
    verdict, _ = _check(ledger)
    if not verdict.complete:
        raise typer.Exit(code=1)
    try:
        out = sign_ledger(ledger, Signer.from_file(key))
    except AttestError as exc:
        console.print(f"[red]{_shown(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"wrote {_shown(str(out))}")


KeyOpt = Annotated[Path | None, typer.Option(help="private key from keygen; signs the output")]


@app.command()
def pack(
    runs_dir: Path,
    out: Annotated[Path, typer.Option(help="evidence pack to write, e.g. audit.seatbelt.zip")],
    key: KeyOpt = None,
    corpus: Annotated[
        Path | None, typer.Option(help="scenario corpus to include; must match findings.json")
    ] = None,
) -> None:
    """Bundle a runs directory into an evidence pack. Refuses a broken ledger."""
    try:
        signer = Signer.from_file(key) if key else None
        manifest = build_pack(runs_dir, out, signer=signer, corpus=corpus)
    except (AttestError, PackError) as exc:
        console.print(f"[red]{_shown(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"wrote {_shown(str(out))}: {len(manifest.runs)} runs, {len(manifest.members)} members"
    )


@app.command(name="verify-pack")
def verify_pack_command(path: Path, pubkey: PubKey = None) -> None:
    """Check an evidence pack offline: manifest, signature, members, chains, attestations.
    With --pubkey, an unsigned pack or a run without its signature file also fails."""
    try:
        verdict = check_pack(path, pubkey)
    except AttestError as exc:
        console.print(f"[red]{_shown(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    if verdict.status is PackStatus.FORGED:
        console.print(f"[red]FORGED[/]: {_shown(verdict.reason or '')}")
        raise typer.Exit(code=1)
    table = Table(Column("run", no_wrap=True), "chain", "attestation")
    for s in verdict.ledgers:
        table.add_row(_shown(s.run_id), s.chain, s.attestation)
    console.print(table)
    if verdict.unsigned:
        console.print("[red]UNSIGNED[/]: a key was given but the pack carries no signature")
    elif verdict.status is not PackStatus.ATTESTED:
        console.print(f"[yellow]{verdict.status.upper()}[/] pack signature not checked")
    for run_id in verdict.unattested:
        console.print(f"[red]UNATTESTED[/]: no runs/{_shown(run_id)}.attest.json")
    if any(s.chain == "broken" for s in verdict.ledgers):
        console.print("[red]BROKEN[/] a ledger in this pack fails its chain check")
    if not verdict.ok:
        raise typer.Exit(code=1)
    console.print(f"[green]ok[/] {len(verdict.ledgers)} runs, pack {verdict.status}")


@app.command()
def demo(out: Path = Path("runs")) -> None:
    """Record a scripted example run so you can try verify and reconstruct."""
    with Recorder.start(out, agent_id="demo-agent", agent_version="0.1") as rec:
        u = rec.user_message("user-42", "Refund order 1001, key sk-ant-abcdefghijklmnopqrstuvwxyz")
        with rec.model_call("claude-sonnet-4-5", {"messages": ["..."]}, provider="anthropic") as m:
            m.respond({"tool_use": "lookup_order"}, usage={"input_tokens": 40, "output_tokens": 12})
        with rec.tool_call("lookup_order", {"order": 1001}) as t:
            t.result({"total": 49.0, "status": "delivered"})
        p = rec.policy_check("refund-limit", u.id, allowed=True, reason="49.00 under 100.00 limit")
        d = rec.decision("issue full refund", authority="agent:auto-under-100", basis=[u.id, p.id])
        rec.action("POST /refunds", target="payments-api", decision_id=d.id)
        rec.outcome("refund issued", success=True)
        console.print(f"wrote {rec.ledger.path}")


def _import_target(spec: str) -> Target:
    sys.path.insert(0, os.getcwd())  # console scripts do not put the cwd on the path
    module, _, attr = spec.partition(":")
    try:  # user code runs at import, so anything can go wrong
        fn = getattr(importlib.import_module(module), attr)
    except Exception as exc:
        console.print(f"[red]cannot import target {escape(spec)}[/]: {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    if not callable(fn):
        console.print(f"[red]target {escape(spec)} is not callable[/]")
        raise typer.Exit(code=2)
    return cast(Target, fn)  # the callable's signature cannot be checked at runtime


@app.command()
def scenarios(
    corpus: Path,
    target: Annotated[
        str | None, typer.Option(help="module:function that drives your agent")
    ] = None,
    out: Path = Path("runs"),
    key: Annotated[
        Path | None, typer.Option(help="private key from keygen; signs each ledger")
    ] = None,
    list_: Annotated[bool, typer.Option("--list", help="show the corpus and exit")] = False,
    image: Annotated[
        str | None,
        typer.Option(help="run each scenario in this Docker image (see docker/Dockerfile)"),
    ] = None,
    target_dir: Annotated[
        Path, typer.Option(help="directory mounted read-only at /target in the sandbox")
    ] = Path("."),
    timeout: Annotated[float, typer.Option(help="seconds per scenario in the sandbox")] = 120,
) -> None:
    """Run the adversarial corpus against a target. Exit 1 on any finding."""
    try:
        pack = load_corpus(corpus)
    except ScenarioError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    if list_:
        table = Table(Column("scenario", no_wrap=True), "owasp", "severity", "egress", "title")
        for s in pack:
            owasp = ", ".join(f"{i} {OWASP_AGENTIC[i]}" for i in s.owasp) or "control"
            egress = "yes" if s.egress else "no"
            table.add_row(s.id, owasp, s.severity, egress, escape(s.title))
        console.print(table)
        return
    if target is None:
        console.print("[red]pass --target module:function, or --list to see the corpus[/]")
        raise typer.Exit(code=2)
    if image and key and key.resolve().is_relative_to(target_dir.resolve()):
        console.print("[red]--key is inside --target-dir and would be mounted into the sandbox[/]")
        raise typer.Exit(code=1)
    fn = _import_target(target) if image is None else None
    try:
        signer = Signer.from_file(key) if key else None
        if fn is not None:
            report = run_corpus(corpus, fn, out, signer=signer)
        else:
            report = run_sandboxed(
                corpus,
                target,
                cast(str, image),  # fn is None only when image was given
                out,
                target_dir=target_dir,
                timeout=timeout,
                signer=signer,
            )
    except (AttestError, LedgerError, ScenarioError, SandboxError) as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    table = Table(Column("scenario", no_wrap=True), "owasp", "severity", "result", "findings")
    for s, r in zip(pack, report.results, strict=True):
        verdict = "[green]PASS[/]" if r.findings == 0 else "[red]FAIL[/]"
        table.add_row(s.id, ", ".join(s.owasp) or "control", s.severity, verdict, str(r.findings))
    console.print(table)
    for f in report.findings:
        evidence = escape(", ".join(f.evidence))
        console.print(f"  {escape(f.scenario_id)}: {escape(f.check)} (evidence: {evidence})")
    console.print(f"wrote {escape(str(out / 'findings.json'))}")
    if report.findings:
        raise typer.Exit(code=1)


gateway = typer.Typer(help="Recording gateway: every employee's model traffic, signed.")
app.add_typer(gateway, name="gateway")

ConfigOpt = Annotated[Path, typer.Option(help="gateway YAML config")]


@gateway.command(name="keygen")
def gateway_keygen(
    user: Annotated[str, typer.Option(help="principal id recorded in every ledger, e.g. email")],
    config: ConfigOpt = Path("gateway.yaml"),
) -> None:
    """Issue a gateway key for a user. Prints it once; the config keeps only its hash."""
    try:
        key = add_principal(config, user)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"key for {escape(user)} (shown once, not stored):")
    console.print(key, highlight=False)


@gateway.command(name="serve")
def gateway_serve(config: ConfigOpt = Path("gateway.yaml")) -> None:
    """Run the gateway. Closes chains a crash left open, then records until stopped, and
    reloads the config when it changes or on SIGHUP."""
    try:
        from seatbelt.gateway.serve import serve  # server deps load only here
    except ImportError as exc:
        hint = (
            "install the gateway extra: pip install 'seatbelt-ai[gateway]'"
            " or uv sync --extra gateway"
        )
        console.print(f"[red]{escape(str(exc))}[/]; {escape(hint)}")
        raise typer.Exit(code=1) from exc
    try:
        serve(config)
    except (AttestError, ValueError) as exc:  # ValueError: the config, or a bad `listen`
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc


@app.command()
def erase(
    folders: Annotated[
        list[Path], typer.Argument(help="runs folders, e.g. the gateway's and the importer's")
    ],
    case: Annotated[
        str, typer.Option(help="case reference, e.g. a ticket number; never the person's name")
    ],
    principal: Annotated[
        list[str] | None, typer.Option(help="a principal id to erase; repeat for several")
    ] = None,
    person: Annotated[str | None, typer.Option(help="a person named in --people")] = None,
    people_file: Annotated[
        Path | None, typer.Option("--people", help="people file, for --person")
    ] = None,
    key: Annotated[
        Path | None, typer.Option(help="private key that signs the erasure record")
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option(help="gateway config: its signing key, and its lines naming the person"),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="erase; without it, only list")] = False,
) -> None:
    """Remove every ledger recorded under a person's principal ids, with its signature and
    shipped mark, inside a signed erasure record. Lists what it would remove unless --yes.
    Nothing may be writing: stop the gateway, and wait for imports and local runs to end."""
    from seatbelt.erase import erase as do_erase
    from seatbelt.erase import live_local_runs, plan
    from seatbelt.gateway.config import load_config
    from seatbelt.gateway.local import login_name
    from seatbelt.gateway.serve import load_signer
    from seatbelt.locks import Busy, hold_folder

    try:
        ids = list(principal or [])
        if person is not None:
            if people_file is None:
                raise ValueError("--person needs --people")
            listed = People.load(people_file).ids_of(person)
            if not listed:
                raise ValueError(f"{people_file}: no ids listed for {person}")
            ids += listed
        ids = sorted(set(ids))
        if not ids:
            raise ValueError("name who to erase: --principal, or --person with --people")
        if not case.strip():
            raise ValueError("--case must not be empty")
        cfg = load_config(config) if config is not None else None
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    console.print(Text(f"principals: {', '.join(ids)}"))
    try:
        plans = [plan(folder, ids) for folder in folders]
    except ValueError as exc:  # an unreadable importer state file
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    for p in plans:
        console.print(Text(f"\n{p.folder}", style="bold"))
        for t in p.targets:
            what = "leftover of an earlier erasure" if t.leftover else "ledger"
            console.print(Text(f"  {what}: {t.ledger.name} ({t.ledger.stat().st_size} bytes)"))
            for k in t.object_keys:
                console.print(Text(f"    in the sink as {k}: not deleted by erase"))
        for o in p.orphans:
            console.print(Text(f"  leftover: {o.relative_to(p.folder)}"))
        if p.state_entries:
            console.print(Text(f"  importer state entries: {len(p.state_entries)}"))
        if p.unmatched:
            console.print(
                Text(f"  {len(p.unmatched)} ledgers name no principal, or unknown: not searched")
            )
        if p.unreadable:
            console.print(Text(f"  unreadable, left alone: {', '.join(p.unreadable)}"))
        if p.empty:
            console.print(Text("  nothing to erase"))
    if cfg is not None:
        for entry in cfg.principals:
            if entry.id in ids:
                console.print(Text(f"\n{config}: remove the key issued to {entry.id}"))
        if cfg.oidc is not None and cfg.oidc.allow:
            for allowed in cfg.oidc.allow:
                if allowed in ids:
                    console.print(Text(f"{config}: remove {allowed} from oidc.allow"))
    if not yes:
        console.print("\nNothing changed. Run again with --yes to erase.")
        return
    try:
        if key is not None:
            signer = Signer.from_file(key)
        elif cfg is not None:
            signer = load_signer(cfg, os.environ)
        else:
            raise ValueError("--yes needs --key, or --config with a signing key")
        # every folder locked before anything changes: nothing may be writing to any of them
        with contextlib.ExitStack() as held:
            for folder in folders:
                held.enter_context(hold_folder(folder, "erase"))
                live = live_local_runs(folder)
                if live:
                    raise Busy(f"{folder}: local runs are recording: {', '.join(live)}")
            for folder in folders:
                r = do_erase(folder, ids, case, signer, login_name())
                if r.closed:
                    console.print(Text(f"{folder}: finished interrupted erasures {r.closed}"))
                if r.record is None:
                    console.print(Text(f"{folder}: nothing to erase"))
                    continue
                console.print(
                    Text(
                        f"{folder}: erased {r.ledgers} ledgers, {r.sidecars} signatures, "
                        f"{r.marks} shipped marks, {r.state_entries} importer entries; "
                        f"record {r.record.name}"
                    )
                )
    except (AttestError, OSError, ValueError) as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc


importer = typer.Typer(help="Import records kept elsewhere into signed ledgers.")
app.add_typer(importer, name="import")


@importer.command(name="compliance")
def import_compliance(config: ConfigOpt = Path("gateway.yaml")) -> None:
    """Import Claude Enterprise transcripts from Anthropic's Compliance API: claude.ai chats,
    and Cowork, Claude Code and other app sessions. Run it on a schedule; each run imports
    what has changed and settled since the last."""
    import logging

    from seatbelt.compliance.client import ComplianceClient, ComplianceError
    from seatbelt.compliance.importer import Busy, Importer
    from seatbelt.gateway.config import load_config
    from seatbelt.gateway.serve import load_signer, make_sink

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")
    try:
        cfg = load_config(config)
        if cfg.compliance is None:
            raise ValueError(f"{config}: add a `compliance:` block to import from the API")
        key = os.environ.get(cfg.compliance.key_env, "").strip()
        if not key:
            raise ValueError(f"set {cfg.compliance.key_env} to a Compliance Access Key")
        signer = load_signer(cfg, os.environ)
        root = cfg.compliance_ledgers
        root.mkdir(parents=True, exist_ok=True)
        sink = make_sink(cfg, os.environ, root)
    except (AttestError, OSError, ValueError) as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    client = ComplianceClient(key, cfg.compliance.url)
    if sink is not None:
        sink.catch_up()
        sink.start()
    try:
        summary = Importer(
            cfg.compliance,
            root,
            client,
            signer,
            on_written=sink.ship if sink is not None else None,
        ).run()
    except (Busy, ComplianceError, ValueError) as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    finally:
        client.close()
        if sink is not None and (left := sink.stop(60)):
            console.print(f"[yellow]{left} ledgers not shipped yet; the next run ships them[/]")
    for source in cfg.compliance.sources:
        name = source.removesuffix("s")
        console.print(
            f"{source}: {summary.listed.get(source, 0)} listed, "
            f"{summary.imported.get(name, 0)} ledgers written, "
            f"{summary.messages.get(name, 0)} messages"
        )
    console.print(f"started {summary.started}; last request-id {summary.last_request_id}")
    console.print(f"ledgers: {root}")
    for closed in summary.closed:
        console.print(f"[yellow]closed {escape(closed)}, left open by a killed import[/]")
    for skipped in summary.skipped:
        console.print(f"[yellow]skipped {escape(skipped)}; retried next run[/]")
    for stuck in summary.stuck:
        console.print(f"[red]{escape(stuck)}: needs a person to check; see the log above[/]")
    if not summary.ok:
        raise typer.Exit(code=1)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    cli: Annotated[str, typer.Argument(help="client to launch: claude, codex or gemini")],
    config: Annotated[
        Path | None,
        typer.Option(
            help="client config (TOML); default ~/.config/seatbelt/config.toml. With no "
            "gateway in it, the run is recorded on this machine"
        ),
    ] = None,
    exe: Annotated[str | None, typer.Option(help="executable, if not the preset's name")] = None,
) -> None:
    """Launch a CLI and record it: on this machine, or through your org's gateway if one is
    configured. Arguments after -- go to the CLI."""
    try:
        code = run_cli(cli, list(ctx.args), config=config, exe=exe)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    except FileNotFoundError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=127) from exc
    raise typer.Exit(code=code)


@app.command()
def report(
    runs: Annotated[
        list[Path] | None,
        typer.Argument(
            help="runs directories, e.g. the gateway's and the importer's; "
            "default: this machine's local runs"
        ),
    ] = None,
    pubkey: PubKey = None,
    people_file: Annotated[
        Path | None,
        typer.Option("--people", help="YAML file joining each person's principal ids (see docs)"),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="print the report as JSON")] = False,
) -> None:
    """Usage across runs directories by person, model and tool, with refused, failed, open and
    unsigned runs. Exit 1 if any ledger is broken or, with --pubkey, forged. With no
    directory: the runs `seatbelt run` recorded here, checked against this machine's key."""
    if not runs:
        try:
            local, pubkey = _local_runs(pubkey)
        except ValueError as exc:  # a bad client config
            console.print(f"[red]{escape(str(exc))}[/]")
            raise typer.Exit(code=1) from exc
        if not any(local.glob("*.jsonl")):
            console.print(f"No runs recorded yet in {local}. Start one with: seatbelt run claude")
            return
        runs = [local]
    try:
        people = People.load(people_file) if people_file is not None else None
        fleet = build_fleet(runs, pubkey, people)
    except (AttestError, ValueError) as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=1) from exc
    if json_out:
        print(fleet.model_dump_json(indent=2))
    else:
        by_person = Table(Column("person", no_wrap=True), "runs", "calls", "in", "out", "denied")
        for name, u in fleet.by_principal.items():
            by_person.add_row(
                Text(name), *map(str, (u.runs, u.calls, u.input_tokens, u.output_tokens, u.denials))
            )
        models = Table(Column("model", no_wrap=True), "runs", "calls", "in", "out", "denied")
        for name, u in fleet.by_model.items():
            models.add_row(
                Text(name), *map(str, (u.runs, u.calls, u.input_tokens, u.output_tokens, u.denials))
            )
        tools = Table(Column("tool", no_wrap=True), "calls")
        for name, n in fleet.by_tool.items():
            tools.add_row(Text(name), str(n))
        for table in (by_person, models, tools):
            console.print(table)
        for person, ids in fleet.people.items():
            console.print(Text(f"{person}: {', '.join(ids)}"))
        console.print(f"{fleet.runs} runs")
        for label, ids in (
            ("failed", fleet.failed),
            ("incomplete", fleet.incomplete),
            ("unattested", fleet.unattested),
            ("forged", fleet.forged),
            ("broken", fleet.broken),
        ):
            if ids:
                console.print(Text(f"{label}: {len(ids)} ({', '.join(ids)})"))
    if fleet.broken or fleet.forged:
        raise typer.Exit(code=1)
