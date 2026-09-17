#!/usr/bin/env bash
# Live CLI session: record a run through the Anthropic adapter, then verify, reconstruct and tamper.
# Usage: scripts/walkthrough.sh [workdir]   (key from ANTHROPIC_API_KEY or ~/.config/anthropic/key)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${1:-$(mktemp -d)}"
mkdir -p "$WORK" && cd "$WORK"

if [[ -z "${ANTHROPIC_API_KEY:-}" && -r ~/.config/anthropic/key ]]; then
  ANTHROPIC_API_KEY="$(cat ~/.config/anthropic/key)"
  export ANTHROPIC_API_KEY
fi
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY or create ~/.config/anthropic/key}"

sb() { uv run --quiet --project "$REPO" seatbelt "$@"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
show() { printf '\033[2m$ %s\033[0m\n' "$*"; }
expect_fail() {
  local name="$1" file="$2"
  if cmp -s "$LEDGER" "$file"; then echo "  $name: tamper did not apply"; FAILED=1; return; fi
  if sb verify "$file" | head -1; then echo "  $name: NOT DETECTED"; FAILED=1; fi
}
FAILED=0

step "1. Record a live run through the adapter"
show "uv run --extra anthropic python examples/anthropic_walkthrough.py"
uv run --quiet --project "$REPO" --extra anthropic python "$REPO/examples/anthropic_walkthrough.py"
LEDGER="$(ls -t runs/*.jsonl | head -1)"

step "2. Verify"
show "seatbelt verify $LEDGER"
sb verify "$LEDGER"

step "3. Reconstruct"
show "seatbelt reconstruct $LEDGER"
COLUMNS=160 sb reconstruct "$LEDGER"

step "4. Redaction"
show "grep -c sk-ant-FAKE $LEDGER"
leaks="$(grep -c 'sk-ant-FAKE' "$LEDGER" || true)"
echo "  fake token occurrences: $leaks (want 0)"
[[ "$leaks" == 0 ]] || FAILED=1

step "5. Lineage"
if ! uv run --quiet --project "$REPO" python - "$LEDGER" <<'EOF'
import json, sys
evs = [json.loads(line) for line in open(sys.argv[1])]
kind = {e["id"]: e["kind"] for e in evs}
want = {"model.response": "model.request", "tool.call": "model.response",
        "tool.result": "tool.call", "policy.check": "user.message", "action": "decision"}
bad = [e["seq"] for e in evs if e["kind"] in want and kind.get(e["parent_id"]) != want[e["kind"]]]
for e in evs:
    print(f'  {e["seq"]:>2} {e["kind"]:<15} parent={kind.get(e["parent_id"])}')
print("  lineage ok" if not bad else f"  lineage BROKEN at seq {bad}")
sys.exit(1 if bad else 0)
EOF
then FAILED=1; fi

step "6. Tamper (every line should be BROKEN or INCOMPLETE)"
mkdir -p tamper
sed 's/\\"total\\": 49.0/\\"total\\": 4900.0/' "$LEDGER" > tamper/edit.jsonl
sed 's/"policy.allowed":true/"policy.allowed":false/' "$LEDGER" > tamper/policy.jsonl
sed '11d' "$LEDGER" > tamper/delete.jsonl
awk 'NR==5{l=$0;next} NR==6{print;print l;next}1' "$LEDGER" > tamper/reorder.jsonl
{ cat "$LEDGER"; echo '{"broken'; } > tamper/corrupt.jsonl
head -n 11 "$LEDGER" > tamper/truncate.jsonl
for t in edit policy delete reorder corrupt truncate; do
  show "seatbelt verify tamper/$t.jsonl"
  expect_fail "$t" "tamper/$t.jsonl"
done

step "7. Attest (signed manifest catches a forged tail the chain accepts)"
sb keygen keys
show "seatbelt attest $LEDGER --key keys/seatbelt.key"
sb attest "$LEDGER" --key keys/seatbelt.key
show "seatbelt verify $LEDGER --pubkey keys/seatbelt.pub"
sb verify "$LEDGER" --pubkey keys/seatbelt.pub
cp "$LEDGER" tamper/forged.jsonl
cp "${LEDGER%.jsonl}.attest.json" tamper/forged.attest.json
uv run --quiet --project "$REPO" python - tamper/forged.jsonl <<'EOF'
import sys
from pathlib import Path
from seatbelt.ledger.events import Actor, ActorType, Kind
from seatbelt.ledger.store import Ledger, read_events
p = Path(sys.argv[1]); lines = p.read_text().splitlines()
p.write_text("\n".join(lines[:-2]) + "\n")
n = sum(1 for _ in read_events(p)) + 1
Ledger(p, next(read_events(p)).run_id).append(Kind.RUN_END, Actor(type=ActorType.AGENT, id="x"), {"run.ok": True, "run.error": None, "run.events": n})
EOF
show "seatbelt verify tamper/forged.jsonl            (chain alone: passes)"
sb verify tamper/forged.jsonl || true
show "seatbelt verify tamper/forged.jsonl --pubkey keys/seatbelt.pub"
if sb verify tamper/forged.jsonl --pubkey keys/seatbelt.pub | head -1; then echo "  forged tail: NOT DETECTED"; FAILED=1; fi

step "8. Evidence pack (one signed zip; verify-pack re-checks every member offline)"
show "seatbelt pack runs --out audit.seatbelt.zip --key keys/seatbelt.key"
sb pack runs --out audit.seatbelt.zip --key keys/seatbelt.key
show "seatbelt verify-pack audit.seatbelt.zip --pubkey keys/seatbelt.pub"
sb verify-pack audit.seatbelt.zip --pubkey keys/seatbelt.pub
uv run --quiet --project "$REPO" python - audit.seatbelt.zip tamper/pack.zip <<'EOF'
import sys, zipfile
src, dst = sys.argv[1:]
with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst, "w") as b:
    for n in a.namelist():
        if not n.endswith(".attest.json"):  # drop every attestation sidecar
            b.writestr(n, a.read(n))
EOF
show "seatbelt verify-pack tamper/pack.zip --pubkey keys/seatbelt.pub   (sidecars removed)"
if sb verify-pack tamper/pack.zip --pubkey keys/seatbelt.pub | head -1; then echo "  pack tamper: NOT DETECTED"; FAILED=1; fi

step "Result"
echo "  workdir: $WORK"
if [[ $FAILED == 0 ]]; then echo "  all checks passed"; else echo "  CHECKS FAILED"; exit 1; fi
