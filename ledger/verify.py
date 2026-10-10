"""Standalone AgentGate ledger auditor.

Usage::

    python ledger/verify.py [chain_path] [public_pem_path]

Defaults: ``chain.jsonl`` next to this script and ``.keys/public.pem`` in the
repository root (the parent of this script's directory).

This script intentionally depends ONLY on the Python standard library
(json, sys, hashlib, base64, pathlib) and
``cryptography.hazmat.primitives.asymmetric.ed25519``. It
imports nothing from the AgentGate project so an auditor can run it in
isolation.

Checks, per entry:

1. the line is valid JSON with the required fields and types;
2. ``index`` is continuous starting at 0;
3. entry 0 is ``genesis`` with ``prev_hash == "0" * 64``;
4. ``prev_hash`` equals the previous entry's ``entry_hash``;
5. ``entry_hash`` equals SHA-256 hex of the canonical JSON
   (``json.dumps(obj, sort_keys=True, separators=(',', ':'))``) of
   ``{"index", "timestamp", "entry_type", "payload", "prev_hash"}``;
6. ``signature`` is base64 of a valid Ed25519 signature over the UTF-8 bytes
   of the hex ``entry_hash`` string, verified against the public key.

Exit status 0 on success; 1 with a ``FAIL: <index and check>`` message otherwise.
"""

import base64
import hashlib
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

GENESIS_PREV_HASH = "0" * 64
# DER prefix of a SubjectPublicKeyInfo wrapping an Ed25519 key (OID 1.3.101.112);
# the 32 raw public key bytes follow it.
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
REQUIRED_FIELDS = (
    ("index", int),
    ("timestamp", str),
    ("entry_type", str),
    ("payload", dict),
    ("prev_hash", str),
    ("entry_hash", str),
    ("signature", str),
)


def canonical(obj):
    """Canonical JSON serialization (must match the ledger writer)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def fail(message):
    """Print a failure message and return exit code 1."""
    print("FAIL: " + message)
    return 1


def load_public_key(path):
    """Load an Ed25519 public key from a SubjectPublicKeyInfo PEM file.

    The PEM body is base64-decoded with the standard library and the DER
    structure is checked against the fixed Ed25519 SubjectPublicKeyInfo prefix,
    so no serialization helpers beyond the ed25519 primitive are needed.
    """
    text = path.read_text(encoding="ascii")
    if "-----BEGIN PUBLIC KEY-----" not in text or "-----END PUBLIC KEY-----" not in text:
        raise ValueError("not a PEM 'PUBLIC KEY' block")
    body = text.split("-----BEGIN PUBLIC KEY-----", 1)[1].split("-----END PUBLIC KEY-----", 1)[0]
    der = base64.b64decode("".join(body.split()), validate=True)
    if len(der) != len(ED25519_SPKI_PREFIX) + 32 or not der.startswith(ED25519_SPKI_PREFIX):
        raise ValueError("public key is not an Ed25519 SubjectPublicKeyInfo key")
    return Ed25519PublicKey.from_public_bytes(der[len(ED25519_SPKI_PREFIX):])


def main(argv):
    script_dir = Path(__file__).resolve().parent
    chain_path = script_dir / "chain.jsonl"
    key_path = script_dir.parent / ".keys" / "public.pem"
    if len(argv) > 1:
        chain_path = Path(argv[1])
    if len(argv) > 2:
        key_path = Path(argv[2])

    if not chain_path.exists():
        return fail("chain file not found: " + str(chain_path))
    if not key_path.exists():
        return fail("public key file not found: " + str(key_path))
    try:
        public_key = load_public_key(key_path)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        return fail("could not load public key " + str(key_path) + ": " + str(exc))

    try:
        raw_lines = chain_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        return fail("chain file is not valid UTF-8: " + str(exc))

    lines = [(lineno, text) for lineno, text in enumerate(raw_lines, start=1) if text.strip()]
    if not lines:
        return fail("chain is empty (no genesis entry): " + str(chain_path))

    expected_index = 0
    prev_entry_hash = None
    head_hash = None
    for lineno, text in lines:
        try:
            entry = json.loads(text)
        except json.JSONDecodeError as exc:
            return fail("line " + str(lineno) + ": malformed JSON (" + str(exc) + ")")
        if not isinstance(entry, dict):
            return fail("line " + str(lineno) + ": entry is not a JSON object")
        for field, ftype in REQUIRED_FIELDS:
            if field not in entry:
                return fail("line " + str(lineno) + ": missing field '" + field + "'")
            value = entry[field]
            if ftype is int and (isinstance(value, bool) or not isinstance(value, int)):
                return fail("line " + str(lineno) + ": field '" + field + "' must be an integer")
            if not isinstance(value, ftype):
                return fail("line " + str(lineno) + ": field '" + field + "' must be of type " + ftype.__name__)

        index = entry["index"]
        label = "index " + str(index) + " (line " + str(lineno) + ")"
        if index != expected_index:
            return fail(
                label + ": index continuity broken, expected index " + str(expected_index) + " but found " + str(index)
            )

        if index == 0:
            if entry["entry_type"] != "genesis":
                return fail(label + ": first entry must be of type 'genesis', found '" + entry["entry_type"] + "'")
            if entry["prev_hash"] != GENESIS_PREV_HASH:
                return fail(label + ": genesis prev_hash must be 64 zeros, found " + entry["prev_hash"])
        else:
            if entry["prev_hash"] != prev_entry_hash:
                return fail(
                    label
                    + ": prev_hash chaining broken, expected "
                    + str(prev_entry_hash)
                    + " but found "
                    + entry["prev_hash"]
                )

        body = {
            "index": entry["index"],
            "timestamp": entry["timestamp"],
            "entry_type": entry["entry_type"],
            "payload": entry["payload"],
            "prev_hash": entry["prev_hash"],
        }
        recomputed = hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()
        if recomputed != entry["entry_hash"]:
            return fail(
                label + ": entry_hash mismatch, expected " + recomputed + " but found " + entry["entry_hash"]
            )

        try:
            signature = base64.b64decode(entry["signature"], validate=True)
        except (ValueError, TypeError) as exc:
            return fail(label + ": signature is not valid base64 (" + str(exc) + ")")
        try:
            public_key.verify(signature, entry["entry_hash"].encode("utf-8"))
        except Exception as exc:  # cryptography raises InvalidSignature on mismatch
            return fail(
                label + ": Ed25519 signature verification failed (" + type(exc).__name__ + ")"
            )

        prev_entry_hash = entry["entry_hash"]
        head_hash = entry["entry_hash"]
        expected_index += 1

    print("OK: ledger verified (" + str(expected_index) + " entries), head hash " + str(head_hash))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
