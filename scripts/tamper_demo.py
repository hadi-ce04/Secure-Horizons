"""Tamper demo: prove the ledger notices a one-character edit.

Never touches the real chain. It copies ``ledger/chain.jsonl`` to a temp file,
flips one character inside the payload of a chosen entry, and runs the
standalone auditor (``ledger/verify.py``) on both files.

    python scripts/tamper_demo.py              # tamper the latest gate_decision
    python scripts/tamper_demo.py --index 7    # tamper entry #7
    python scripts/tamper_demo.py --field decision --to ALLOW   # BLOCK -> ALLOW rewrite

Exit code 0 means the demo behaved as expected (original verifies, tampered
copy fails). Anything else is a real problem.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHAIN = REPO_ROOT / "ledger" / "chain.jsonl"
PUBLIC_KEY = REPO_ROOT / ".keys" / "public.pem"
VERIFY = REPO_ROOT / "ledger" / "verify.py"


def run_verify(chain: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(VERIFY), str(chain), str(PUBLIC_KEY)],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode, (out[-1] if out else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", type=int, default=None, help="entry index to tamper (default: latest gate_decision)")
    ap.add_argument("--field", default="decision", help="payload field to rewrite (default: decision)")
    ap.add_argument("--to", default="ALLOW", help="new value for the field (default: ALLOW)")
    args = ap.parse_args(argv)

    entries = [json.loads(line) for line in CHAIN.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.index is None:
        candidates = [e for e in entries if e["entry_type"] == "gate_decision" and e["payload"].get(args.field) != args.to]
        if not candidates:
            print("no gate_decision entry to tamper; run a check first")
            return 2
        target = candidates[-1]
    else:
        target = next((e for e in entries if e["index"] == args.index), None)
        if target is None:
            print(f"no entry with index {args.index}")
            return 2

    before = target["payload"].get(args.field)
    print(f"Target: entry #{target['index']} ({target['entry_type']}), payload.{args.field} = {before!r}")
    print(f"Rewriting to {args.to!r} in a COPY of the chain (the real chain is not modified).")

    with tempfile.TemporaryDirectory() as tmp:
        tampered = Path(tmp) / "chain.tampered.jsonl"
        shutil.copy(CHAIN, tampered)
        lines = tampered.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            e = json.loads(line)
            if e["index"] == target["index"]:
                e["payload"][args.field] = args.to
                lines[i] = json.dumps(e, sort_keys=True, separators=(",", ":"))
        tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")

        code_orig, msg_orig = run_verify(CHAIN)
        code_tamp, msg_tamp = run_verify(tampered)

    print()
    print(f"original chain : exit {code_orig}  {msg_orig}")
    print(f"tampered copy  : exit {code_tamp}  {msg_tamp}")
    ok = code_orig == 0 and code_tamp != 0
    print()
    print("RESULT: tampering detected" if ok else "RESULT: UNEXPECTED (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
