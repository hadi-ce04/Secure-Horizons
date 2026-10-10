"""Append-only, Ed25519-signed hash-chain ledger stored as JSONL.

Each line of the chain file is one canonical JSON object::

    {"index": int, "timestamp": str, "entry_type": str, "payload": dict,
     "prev_hash": str, "entry_hash": str, "signature": str}

Hashing and signing rules (``ledger/verify.py`` re-implements them exactly):

* canonical JSON = ``json.dumps(obj, sort_keys=True, separators=(',', ':'))``
* ``entry_hash`` = SHA-256 hex digest of the canonical JSON of
  ``{"index", "timestamp", "entry_type", "payload", "prev_hash"}``
* ``signature`` = base64 of the Ed25519 signature over the UTF-8 bytes of the
  64-character hex ``entry_hash`` string.
* The genesis entry has ``index == 0``, ``entry_type == "genesis"`` and
  ``prev_hash == "0" * 64``.

Concurrency: every write (genesis, append, conditional append) runs under an
exclusive lock so that separate processes (for example a ``monitor --interval``
loop and a CLI or dashboard ``check``) never read the same head and write
duplicate indices. The lock is an OS-level lock on a dedicated sibling file
``<chain>.lock`` (``msvcrt.locking`` on Windows, ``fcntl.flock`` on POSIX),
held across read-head -> build -> write -> fsync, plus a process-wide
``threading.Lock`` for threads. Readers of the chain are never blocked. If the
lock cannot be acquired within ``lock_timeout`` seconds (default 10), a
``LedgerError`` is raised.

This module depends only on the standard library and ``cryptography``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHAIN_PATH = REPO_ROOT / "ledger" / "chain.jsonl"
DEFAULT_KEYS_DIR = REPO_ROOT / ".keys"
GENESIS_PREV_HASH = "0" * 64
ENTRY_TYPES = {
    "genesis",
    "check_run",
    "gate_decision",
    "override",
    "pack_generated",
    "monitor_run",
    "staleness_alert",
}

DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_BACKOFF_START = 0.005
_LOCK_BACKOFF_MAX = 0.05

# Process-wide (not per instance) so two Ledger objects on the same chain in one
# process never compete for the OS lock through two handles. Not re-entrant.
_WRITE_LOCK = threading.Lock()


def canonical_json(obj) -> str:
    """Return the canonical JSON serialization used for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256_hex(s: str) -> str:
    """Return the SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    """Return the current UTC time as ISO-8601 with microseconds and a Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_timestamp(ts: str) -> datetime:
    """Parse a ledger timestamp into an aware UTC datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def compute_entry_hash(index: int, timestamp: str, entry_type: str, payload: dict, prev_hash: str) -> str:
    """Compute the entry hash over the canonical hashed fields."""
    body = {
        "index": index,
        "timestamp": timestamp,
        "entry_type": entry_type,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    return sha256_hex(canonical_json(body))


class LedgerError(Exception):
    """Raised for ledger integrity, key, or usage errors."""


class Ledger:
    """A signed hash-chain ledger bound to a chain file and a keys directory."""

    def __init__(
        self,
        chain_path: Path | str = DEFAULT_CHAIN_PATH,
        keys_dir: Path | str = DEFAULT_KEYS_DIR,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ):
        self.chain_path = Path(chain_path)
        self.keys_dir = Path(keys_dir)
        self.private_key_path = self.keys_dir / "private.pem"
        self.public_key_path = self.keys_dir / "public.pem"
        self.lock_path = self.chain_path.with_name(self.chain_path.name + ".lock")
        self.lock_timeout = lock_timeout
        self._private_key: Ed25519PrivateKey | None = None

    # --------------------------------------------------------------- locking
    @staticmethod
    def _try_lock(fd: int) -> bool:
        """Try once to take the exclusive OS lock on fd; return True on success."""
        try:
            if os.name == "nt":
                # Lock byte 0 (locking past EOF is allowed, so the file can stay empty).
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _acquire_file_lock(self) -> int:
        """Open the lock file and take an exclusive lock, retrying until the timeout."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + self.lock_timeout
            delay = _LOCK_BACKOFF_START
            while not self._try_lock(fd):
                if time.monotonic() >= deadline:
                    raise LedgerError(
                        f"Timed out after {self.lock_timeout:.1f}s waiting for ledger lock {self.lock_path}; "
                        f"another AgentGate process may be writing to {self.chain_path}"
                    )
                time.sleep(delay)
                delay = min(delay * 2, _LOCK_BACKOFF_MAX)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _release_file_lock(fd: int) -> None:
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Hold the thread lock and the cross-process file lock. Must not be nested."""
        with _WRITE_LOCK:
            fd = self._acquire_file_lock()
            try:
                yield
            finally:
                self._release_file_lock(fd)

    # ------------------------------------------------------------------ keys
    def _chain_has_entries(self) -> bool:
        if not self.chain_path.exists():
            return False
        with open(self.chain_path, "r", encoding="utf-8") as fh:
            return any(line.strip() for line in fh)

    def ensure_keys(self) -> Ed25519PrivateKey:
        """Load the Ed25519 keypair, generating it on first use.

        Refuses to generate a new keypair when the chain already has entries,
        because new keys would make every existing signature unverifiable.
        """
        if self._private_key is not None:
            return self._private_key
        priv_exists = self.private_key_path.exists()
        pub_exists = self.public_key_path.exists()
        if not priv_exists and not pub_exists:
            if self._chain_has_entries():
                raise LedgerError(
                    f"Ledger {self.chain_path} already has entries but keys are missing in "
                    f"{self.keys_dir}; refusing to generate a new keypair (it would invalidate the chain)."
                )
            self.keys_dir.mkdir(parents=True, exist_ok=True)
            key = Ed25519PrivateKey.generate()
            priv_pem = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            pub_pem = key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            with open(self.private_key_path, "wb") as fh:
                fh.write(priv_pem)
            with open(self.public_key_path, "wb") as fh:
                fh.write(pub_pem)
            self._private_key = key
            return key
        if not priv_exists:
            raise LedgerError(f"Private key missing: {self.private_key_path} (public key exists)")
        with open(self.private_key_path, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise LedgerError(f"{self.private_key_path} is not an Ed25519 private key")
        if not pub_exists:
            pub_pem = key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            with open(self.public_key_path, "wb") as fh:
                fh.write(pub_pem)
        self._private_key = key
        return key

    def public_key(self) -> Ed25519PublicKey:
        """Return the ledger's Ed25519 public key."""
        return self.ensure_keys().public_key()

    def public_key_fingerprint(self) -> str:
        """Return the SHA-256 hex digest of the raw public key bytes."""
        raw = self.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return hashlib.sha256(raw).hexdigest()

    # --------------------------------------------------------------- reading
    def read_all(self) -> list[dict]:
        """Return every entry in order. Raises LedgerError on malformed JSON."""
        if not self.chain_path.exists():
            return []
        entries: list[dict] = []
        with open(self.chain_path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LedgerError(f"Malformed JSON on line {lineno} of {self.chain_path}: {exc}") from exc
                if not isinstance(entry, dict):
                    raise LedgerError(f"Line {lineno} of {self.chain_path} is not a JSON object")
                entries.append(entry)
        return entries

    def head(self) -> dict | None:
        """Return the last entry, or None if the chain is empty."""
        entries = self.read_all()
        return entries[-1] if entries else None

    def head_hash(self) -> str | None:
        """Return the entry_hash of the last entry, or None if empty."""
        h = self.head()
        return h["entry_hash"] if h else None

    def latest(self, entry_type: str, predicate: Callable[[dict], bool] | None = None) -> dict | None:
        """Return the most recent entry of a type that satisfies an optional predicate."""
        for entry in reversed(self.read_all()):
            if entry.get("entry_type") != entry_type:
                continue
            if predicate is None or predicate(entry):
                return entry
        return None

    # --------------------------------------------------------------- writing
    def _build_entry(self, index: int, entry_type: str, payload: dict, prev_hash: str) -> dict:
        key = self.ensure_keys()
        normalized = json.loads(canonical_json(payload))
        timestamp = utc_now_iso()
        entry_hash = compute_entry_hash(index, timestamp, entry_type, normalized, prev_hash)
        signature = base64.b64encode(key.sign(entry_hash.encode("utf-8"))).decode("ascii")
        return {
            "index": index,
            "timestamp": timestamp,
            "entry_type": entry_type,
            "payload": normalized,
            "prev_hash": prev_hash,
            "entry_hash": entry_hash,
            "signature": signature,
        }

    def _write_entry(self, entry: dict) -> None:
        self.chain_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.chain_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(canonical_json(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _init_genesis_locked(self) -> dict:
        entries = self.read_all()
        if entries:
            return entries[0]
        self.ensure_keys()
        payload = {
            "message": "AgentGate ledger genesis",
            "public_key_sha256": self.public_key_fingerprint(),
        }
        entry = self._build_entry(0, "genesis", payload, GENESIS_PREV_HASH)
        self._write_entry(entry)
        return entry

    def init_genesis(self) -> dict:
        """Create the genesis entry if the chain is empty; return the genesis entry."""
        with self._exclusive():
            return self._init_genesis_locked()

    @staticmethod
    def _check_entry_type(entry_type: str) -> None:
        if entry_type not in ENTRY_TYPES or entry_type == "genesis":
            raise LedgerError(f"Invalid entry_type {entry_type!r}; expected one of {sorted(ENTRY_TYPES - {'genesis'})}")

    def append_with(self, entry_type: str, build_payload: Callable[[list[dict]], dict]) -> dict:
        """Atomically build and append a signed entry; return it.

        Under the ledger lock, ``build_payload`` receives the current entries
        (freshly read from disk) and returns the payload dict. If it raises, the
        exception propagates and nothing is written, so callers can implement
        check-then-append rules without races. The lock is not reentrant:
        ``build_payload`` must not call ``append``/``append_with``/``init_genesis``
        (that would deadlock).
        """
        self._check_entry_type(entry_type)
        with self._exclusive():
            self._init_genesis_locked()
            entries = self.read_all()
            payload = build_payload(entries)
            if not isinstance(payload, dict):
                raise LedgerError("Ledger payload must be a dict")
            head = entries[-1]
            entry = self._build_entry(int(head["index"]) + 1, entry_type, payload, head["entry_hash"])
            self._write_entry(entry)
            return entry

    def append(self, entry_type: str, payload: dict) -> dict:
        """Append a signed entry and return it."""
        self._check_entry_type(entry_type)
        if not isinstance(payload, dict):
            raise LedgerError("Ledger payload must be a dict")
        return self.append_with(entry_type, lambda _entries: payload)


_DEFAULT_LEDGER: Ledger | None = None


def get_default_ledger() -> Ledger:
    """Return the process-wide ledger bound to the repo's chain and keys."""
    global _DEFAULT_LEDGER
    if _DEFAULT_LEDGER is None:
        _DEFAULT_LEDGER = Ledger()
    return _DEFAULT_LEDGER


def ensure_keys() -> Ed25519PrivateKey:
    """Ensure the default keypair exists."""
    return get_default_ledger().ensure_keys()


def init_genesis() -> dict:
    """Initialize the default chain with a genesis entry (idempotent)."""
    return get_default_ledger().init_genesis()


def append(entry_type: str, payload: dict) -> dict:
    """Append to the default chain."""
    return get_default_ledger().append(entry_type, payload)


def append_with(entry_type: str, build_payload: Callable[[list[dict]], dict]) -> dict:
    """Atomically build and append to the default chain."""
    return get_default_ledger().append_with(entry_type, build_payload)


def read_all() -> list[dict]:
    """Read every entry of the default chain."""
    return get_default_ledger().read_all()


def head() -> dict | None:
    """Return the head of the default chain."""
    return get_default_ledger().head()


def head_hash() -> str | None:
    """Return the head hash of the default chain."""
    return get_default_ledger().head_hash()
