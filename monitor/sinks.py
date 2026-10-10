"""Alert sinks for the monitor: forward alerts to a JSONL file and/or a webhook.

The ledger remains the system of record (every alert is also a signed ledger
entry). Sinks exist so a SIEM / SOAR / chat tool can pick alerts up without
reading the chain: a JSONL file for log shippers (Splunk, Elastic, Datadog
agents tail it) and an HTTP POST for webhooks (Slack, PagerDuty, SOAR intake).

The webhook payload is a single JSON object per alert. Delivery failures never
abort the monitor; they are returned in the result so the caller can print them.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_WEBHOOK_TIMEOUT = 10.0
ENV_ALERT_JSONL = "AGENTGATE_ALERT_JSONL"
ENV_ALERT_WEBHOOK = "AGENTGATE_ALERT_WEBHOOK"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_event(alert: dict, monitor_entry_hash: str | None = None, source: str = "agentgate-monitor") -> dict:
    """Flat, SIEM-friendly event: stable top-level keys, original alert nested under ``alert``."""
    return {
        "timestamp": _utc_now_iso(),
        "source": source,
        "kind": alert.get("kind"),
        "severity": "high" if alert.get("kind") in ("asr_drift", "config_changed") else "medium",
        "message": alert.get("message"),
        "config_name": alert.get("config_name"),
        "config_hash": alert.get("config_hash"),
        "monitor_entry_hash": monitor_entry_hash,
        "alert": alert,
    }


def write_jsonl(events: list[dict], path: Path | str) -> int:
    """Append events as JSON lines; returns the number written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        for ev in events:
            fh.write(json.dumps(ev, sort_keys=True) + "\n")
    return len(events)


def post_webhook(events: list[dict], url: str, timeout: float = DEFAULT_WEBHOOK_TIMEOUT) -> list[str]:
    """POST each event to ``url``; returns a list of error strings (empty on full success)."""
    import requests  # lazy: only needed when a webhook is configured

    errors: list[str] = []
    for ev in events:
        try:
            resp = requests.post(url, json=ev, timeout=timeout)
            if resp.status_code >= 300:
                errors.append(f"webhook {url} returned HTTP {resp.status_code} for alert {ev.get('kind')}")
        except requests.RequestException as exc:  # network errors must not stop the monitor
            errors.append(f"webhook {url} failed for alert {ev.get('kind')}: {type(exc).__name__}")
    return errors


def emit_alerts(
    alerts: list[dict],
    monitor_entry_hash: str | None = None,
    jsonl_path: Path | str | None = None,
    webhook_url: str | None = None,
) -> dict:
    """Forward alerts to the configured sinks. Env vars fill in unset arguments.

    Returns ``{"events": n, "jsonl": path_or_None, "webhook": url_or_None, "errors": [...]}``.
    """
    jsonl_path = jsonl_path or os.environ.get(ENV_ALERT_JSONL) or None
    webhook_url = webhook_url or os.environ.get(ENV_ALERT_WEBHOOK) or None
    events = [build_event(a, monitor_entry_hash) for a in alerts]
    result = {"events": len(events), "jsonl": str(jsonl_path) if jsonl_path else None,
              "webhook": webhook_url, "errors": []}
    if not events:
        return result
    if jsonl_path:
        write_jsonl(events, jsonl_path)
    if webhook_url:
        result["errors"] = post_webhook(events, webhook_url)
    return result
