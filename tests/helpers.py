"""Shared test helpers: temp ledger/cache paths and a verify.py runner."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from ledger.ledger import Ledger

REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = REPO_ROOT / "ledger" / "verify.py"


class TempWorkspace:
    """Temporary directory holding an isolated ledger, keys, cache, and outputs."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.chain = self.root / "chain.jsonl"
        self.keys = self.root / "keys"
        self.cache = self.root / "cache"
        self.latest = self.root / "latest_check.json"
        self.packs = self.root / "packs"
        self.ledger = Ledger(self.chain, self.keys)

    def verify(self, chain: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), str(chain or self.chain), str(self.keys / "public.pem")],
            capture_output=True,
            text=True,
        )

    def cleanup(self) -> None:
        self._tmp.cleanup()


# Multiprocessing targets live at module level so the Windows "spawn" start
# method can import and pickle them.
def concurrent_append_worker(chain: str, keys: str, worker_id: int, count: int, barrier) -> None:
    ledger = Ledger(chain, keys)
    barrier.wait()
    for i in range(count):
        ledger.append("check_run", {"worker": worker_id, "i": i})


def hold_ledger_lock(chain: str, keys: str, ready, release, max_seconds: float) -> None:
    ledger = Ledger(chain, keys)
    with ledger._exclusive():
        ready.set()
        release.wait(max_seconds)
