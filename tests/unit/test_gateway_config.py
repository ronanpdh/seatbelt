from pathlib import Path

import pytest

from seatbelt.gateway.config import add_principal, key_hash, load_config

MINIMAL = """
signing_key: keys/seatbelt.key
ledgers: runs
upstreams:
  anthropic: {url: https://api.anthropic.com, key_env: ANTHROPIC_API_KEY}
"""


def test_defaults_and_required(tmp_path: Path) -> None:
    cfg_path = tmp_path / "gateway.yaml"
    cfg_path.write_text(MINIMAL)
    cfg = load_config(cfg_path)
    assert cfg.listen == "127.0.0.1:8080" and cfg.session_idle == 900
    assert cfg.signing_key == tmp_path / "keys" / "seatbelt.key"  # relative to the file
    assert cfg.ledgers == tmp_path / "runs"
    assert cfg.principals == [] and cfg.policy.models is None
    assert cfg.upstreams["anthropic"].key_env == "ANTHROPIC_API_KEY"
    cfg_path.write_text("signing_key: k\nledgers: /var/x\nupstreams: {}\n")
    assert load_config(cfg_path).ledgers == Path("/var/x")  # absolute stays absolute


def test_unknown_key_missing_file_and_bad_yaml_are_rejected(tmp_path: Path) -> None:
    p = tmp_path / "g.yaml"
    p.write_text(MINIMAL + "surprise: 1\n")
    with pytest.raises(ValueError, match=r"g\.yaml"):
        load_config(p)
    with pytest.raises(ValueError, match=r"missing\.yaml"):
        load_config(tmp_path / "missing.yaml")
    p.write_text("upstreams: [not a mapping\n")
    with pytest.raises(ValueError, match=r"g\.yaml"):
        load_config(p)
    p.write_text(MINIMAL + "principals: [{id: a, key_sha256: ZZZ, issued: 2026-09-18}]\n")
    with pytest.raises(ValueError, match=r"g\.yaml"):
        load_config(p)


def test_add_principal_prints_key_once_and_stores_only_the_hash(tmp_path: Path) -> None:
    p = tmp_path / "g.yaml"
    p.write_text(MINIMAL)
    secret = add_principal(p, "alice@corp")
    assert secret.startswith("sbk_") and len(secret) > 40
    text = p.read_text()
    assert secret not in text and key_hash(secret) in text
    cfg = load_config(p)
    found = cfg.lookup(secret)
    assert found is not None and found.id == "alice@corp"
    assert cfg.lookup("sbk_wrong") is None and cfg.lookup("") is None
    with pytest.raises(ValueError, match="alice@corp"):
        add_principal(p, "alice@corp")
    second = add_principal(p, "bob@corp")
    cfg = load_config(p)
    assert [q.id for q in cfg.principals] == ["alice@corp", "bob@corp"]
    assert cfg.lookup(second) is not None and cfg.lookup(secret) is not None


def test_add_principal_keeps_the_file_readable_and_paths_relative(tmp_path: Path) -> None:
    p = tmp_path / "g.yaml"
    p.write_text(MINIMAL)
    add_principal(p, "alice@corp")
    assert "signing_key: keys/seatbelt.key" in p.read_text()  # not resolved to absolute
