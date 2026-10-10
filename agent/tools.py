"""Tool implementations bound to a per-trial Environment.

``Toolbox`` wraps one ``Environment``; there is no global mutable trial state.
``send_email`` records to the trial's in-memory outbox (used by the judge) and
also appends to ``agent/outbox.jsonl`` for audit, guarded by a file lock.
"""

from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from agent.environment import Environment

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTBOX_LOG = REPO_ROOT / "agent" / "outbox.jsonl"
_OUTBOX_FILE_LOCK = threading.Lock()

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Read a single email from the user's inbox by its id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Email id, e.g. email-001."},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "Search internal documents. An empty query returns all documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an email on behalf of the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient address(es), comma separated."},
                    "subject": {"type": "string", "description": "Subject line."},
                    "body": {"type": "string", "description": "Plain-text body."},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
]

TOOL_SCHEMAS_BY_NAME: dict[str, dict] = {s["function"]["name"]: s for s in TOOL_SCHEMAS}


def get_tool_schemas(names) -> list[dict]:
    """Return deep copies of the schemas for ``names`` in the given order."""
    result = []
    for name in names:
        if name not in TOOL_SCHEMAS_BY_NAME:
            raise KeyError(f"Unknown tool {name!r}; available tools: {sorted(TOOL_SCHEMAS_BY_NAME)}")
        result.append(copy.deepcopy(TOOL_SCHEMAS_BY_NAME[name]))
    return result


class Toolbox:
    """Tool implementations operating on one Environment instance."""

    def __init__(self, env: Environment, outbox_log_path: Path | str | None = DEFAULT_OUTBOX_LOG):
        self.env = env
        self.outbox_log_path = Path(outbox_log_path) if outbox_log_path is not None else None

    def read_email(self, id: str) -> dict:  # noqa: A002 - name matches the tool schema
        """Return the email with this id or an error dict."""
        email = self.env.get_email(id)
        if email is None:
            return {"error": f"email not found: {id}"}
        return copy.deepcopy(email)

    def search_docs(self, query: str) -> list[dict]:
        """Case-insensitive token search over doc titles and contents."""
        q = (query or "").strip().lower()
        if not q:
            return copy.deepcopy(self.env.docs)
        tokens = q.split()
        hits = []
        for doc in self.env.docs:
            haystack = (doc["title"] + "\n" + doc["content"]).lower()
            if any(tok in haystack for tok in tokens):
                hits.append(copy.deepcopy(doc))
        return hits

    def send_email(self, to: str, subject: str, body: str) -> dict:
        """Record an outgoing email in the trial outbox and the audit log."""
        record = {
            "to": to,
            "subject": subject,
            "body": body,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "trial_id": self.env.trial_id,
        }
        self.env.outbox.append(record)
        if self.outbox_log_path is not None:
            with _OUTBOX_FILE_LOCK:
                self.outbox_log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.outbox_log_path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(record, sort_keys=True) + "\n")
        return {"status": "sent", "to": to}

    def dispatch(self, name: str, arguments) -> object:
        """Invoke a tool by name with a dict or JSON-string of arguments."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                return {"error": f"invalid JSON arguments for {name}: {exc}"}
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return {"error": f"arguments for {name} must be an object"}
        handlers = {
            "read_email": self.read_email,
            "search_docs": self.search_docs,
            "send_email": self.send_email,
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": f"unknown tool: {name}"}
        try:
            return handler(**arguments)
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
