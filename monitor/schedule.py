"""Scheduled suite re-runs and staleness alerting.

Alerts:

* ``no_evaluation``    - no prior evaluation exists for the config hash;
* ``stale_evaluation`` - the latest evaluation for the hash is older than max-age;
* ``config_changed``   - the latest evaluation for the same config path had a different hash;
* ``asr_drift``        - a category's ASR moved by more than ``DRIFT_THRESHOLD`` versus the
  latest evaluation with the same hash and mode.

Evaluations are read from ``check_run`` (baseline and candidate sides) and
``monitor_run`` ledger entries. Scheduling uses a plain ``time.sleep`` loop.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from agent.tools import DEFAULT_OUTBOX_LOG
from gate.config import AgentConfig, load_config
from gate.run import DEFAULT_CACHE_DIR, DEFAULT_CONCURRENCY, DEFAULT_TRIALS_PER_ATTACK, load_suite, run_suite
from ledger.ledger import Ledger, get_default_ledger, parse_timestamp
from monitor.sinks import emit_alerts

DEFAULT_MAX_AGE_HOURS = 24.0
DRIFT_THRESHOLD = 0.10
DEFAULT_INTERVAL_SECONDS = 3600
DEFAULT_MONITOR_CONFIG = "configs/v1.yaml"


def _evaluations(entries: list[dict], config_path: str | None = None, config_hash: str | None = None) -> list[dict]:
    """Extract evaluation records from ledger entries, optionally filtered."""
    evals: list[dict] = []
    for entry in entries:
        etype = entry.get("entry_type")
        payload = entry.get("payload", {})
        if etype == "check_run":
            for side in ("baseline", "candidate"):
                s = payload.get(side) or {}
                evals.append(
                    {
                        "timestamp": entry["timestamp"],
                        "config_path": s.get("path"),
                        "config_hash": s.get("hash"),
                        "mode": payload.get("mode"),
                        "summary": s.get("summary"),
                        "entry_hash": entry["entry_hash"],
                    }
                )
        elif etype == "monitor_run":
            evals.append(
                {
                    "timestamp": entry["timestamp"],
                    "config_path": payload.get("config_path"),
                    "config_hash": payload.get("config_hash"),
                    "mode": payload.get("mode"),
                    "summary": payload.get("summary"),
                    "entry_hash": entry["entry_hash"],
                }
            )
    if config_path is not None:
        evals = [e for e in evals if e["config_path"] == config_path]
    if config_hash is not None:
        evals = [e for e in evals if e["config_hash"] == config_hash]
    return evals


def _alert(kind: str, message: str, config: AgentConfig, details: dict) -> dict:
    return {
        "kind": kind,
        "message": message,
        "config_path": config.path,
        "config_hash": config.config_hash,
        "details": details,
    }


def check_staleness(
    config: AgentConfig,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
    ledger: Ledger | None = None,
) -> list[dict]:
    """Return staleness alerts for a config based on prior ledger evaluations."""
    ledger = ledger or get_default_ledger()
    now = now or datetime.now(timezone.utc)
    entries = ledger.read_all()
    alerts: list[dict] = []
    chash = config.config_hash

    same_path = _evaluations(entries, config_path=config.path) if config.path else []
    if same_path and same_path[-1]["config_hash"] != chash:
        prev = same_path[-1]
        alerts.append(
            _alert(
                "config_changed",
                f"{config.path} hash changed since last evaluation: {str(prev['config_hash'])[:12]} -> {chash[:12]}",
                config,
                {"previous_hash": prev["config_hash"], "previous_entry_hash": prev["entry_hash"]},
            )
        )

    same_hash = _evaluations(entries, config_hash=chash)
    if not same_hash:
        alerts.append(
            _alert(
                "no_evaluation",
                f"No prior evaluation recorded for {config.name} ({chash[:12]})",
                config,
                {},
            )
        )
    else:
        last = same_hash[-1]
        age_hours = (now - parse_timestamp(last["timestamp"])).total_seconds() / 3600.0
        if age_hours > max_age_hours:
            alerts.append(
                _alert(
                    "stale_evaluation",
                    f"Last evaluation of {config.name} ({chash[:12]}) is {age_hours:.2f}h old (max {max_age_hours:g}h)",
                    config,
                    {"age_hours": age_hours, "max_age_hours": max_age_hours, "last_entry_hash": last["entry_hash"]},
                )
            )
    return alerts


def detect_drift(
    config: AgentConfig,
    mode: str,
    new_summary: dict,
    ledger: Ledger | None = None,
    entries: list[dict] | None = None,
) -> list[dict]:
    """Compare a new summary to the latest evaluation with the same hash and mode."""
    ledger = ledger or get_default_ledger()
    entries = entries if entries is not None else ledger.read_all()
    prior = [e for e in _evaluations(entries, config_hash=config.config_hash) if e["mode"] == mode and e["summary"]]
    if not prior:
        return []
    prev = prior[-1]
    prev_cats = prev["summary"].get("categories", {})
    alerts = []
    for cat, stats in new_summary.get("categories", {}).items():
        if cat not in prev_cats:
            continue
        delta = float(stats["asr"]) - float(prev_cats[cat]["asr"])
        if abs(delta) > DRIFT_THRESHOLD:
            alerts.append(
                _alert(
                    "asr_drift",
                    f"{config.name} {cat} ASR drifted {prev_cats[cat]['asr']:.3f} -> {stats['asr']:.3f} "
                    f"(|delta| {abs(delta):.3f} > {DRIFT_THRESHOLD:.2f})",
                    config,
                    {
                        "category": cat,
                        "previous_asr": float(prev_cats[cat]["asr"]),
                        "new_asr": float(stats["asr"]),
                        "delta": delta,
                        "previous_entry_hash": prev["entry_hash"],
                    },
                )
            )
    return alerts


def run_monitor_once(
    config_path=DEFAULT_MONITOR_CONFIG,
    mode: str = "offline",
    trials_per_attack: int = DEFAULT_TRIALS_PER_ATTACK,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    concurrency: int = DEFAULT_CONCURRENCY,
    use_cache: bool = False,
    ollama_model: str | None = None,
    ledger: Ledger | None = None,
    cache_dir=DEFAULT_CACHE_DIR,
    outbox_log_path=DEFAULT_OUTBOX_LOG,
    now: datetime | None = None,
    alert_jsonl=None,
    alert_webhook: str | None = None,
) -> dict:
    """Run one monitoring pass: staleness check, suite run, drift check, ledger logging, alert sinks."""
    ledger = ledger or get_default_ledger()
    config = load_config(config_path)
    alerts = check_staleness(config, max_age_hours=max_age_hours, now=now, ledger=ledger)
    entries_before = ledger.read_all()
    suite = load_suite()
    result = run_suite(
        config,
        suite,
        mode=mode,
        trials_per_attack=trials_per_attack,
        concurrency=concurrency,
        use_cache=use_cache,
        ollama_model=ollama_model,
        cache_dir=cache_dir,
        outbox_log_path=outbox_log_path,
    )
    alerts += detect_drift(config, mode, result["summary"], ledger=ledger, entries=entries_before)
    entry = ledger.append(
        "monitor_run",
        {
            "config_path": config.path,
            "config_name": config.name,
            "config_hash": config.config_hash,
            "mode": mode,
            "model": result["model"],
            "trials_per_attack": trials_per_attack,
            "summary": result["summary"],
            "alert_count": len(alerts),
        },
    )
    for alert in alerts:
        ledger.append("staleness_alert", dict(alert, monitor_run_entry_hash=entry["entry_hash"]))
    sink_result = emit_alerts(alerts, monitor_entry_hash=entry["entry_hash"],
                              jsonl_path=alert_jsonl, webhook_url=alert_webhook)
    return {
        "monitor_entry_hash": entry["entry_hash"],
        "monitor_index": entry["index"],
        "config": {"name": config.name, "path": config.path, "hash": config.config_hash},
        "summary": result["summary"],
        "alerts": alerts,
        "sinks": sink_result,
    }


def run_monitor_loop(
    config_path=DEFAULT_MONITOR_CONFIG,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    iterations: int | None = None,
    printer: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs,
) -> list[dict]:
    """Run ``run_monitor_once`` repeatedly every ``interval_seconds``."""
    if interval_seconds < 0:
        raise ValueError("interval_seconds must be >= 0")
    results = []
    count = 0
    while iterations is None or count < iterations:
        res = run_monitor_once(config_path=config_path, **kwargs)
        results.append(res)
        count += 1
        printer(format_run(res))
        for alert in res["alerts"]:
            printer(format_alert(alert))
        sinks = res.get("sinks") or {}
        if sinks.get("events"):
            targets = [t for t in (sinks.get("jsonl"), sinks.get("webhook")) if t]
            printer(f"forwarded {sinks['events']} alert(s) to: {', '.join(targets) if targets else 'no sink configured'}")
        for err in sinks.get("errors", []):
            printer(f"SINK ERROR: {err}")
        if iterations is not None and count >= iterations:
            break
        sleep(interval_seconds)
    return results


def format_run(res: dict) -> str:
    """One-line ASCII summary of a monitor run."""
    overall = res["summary"]["overall"]
    return (
        f"monitor_run {res['config']['name']} ({res['config']['hash'][:12]}): overall ASR {overall['asr']:.3f} "
        f"CI [{overall['ci_lower']:.3f}, {overall['ci_upper']:.3f}] n={overall['n']} "
        f"alerts={len(res['alerts'])} entry {res['monitor_entry_hash'][:16]}"
    )


def format_alert(alert: dict) -> str:
    """ASCII alert line."""
    return f"ALERT [{alert['kind']}] {alert['message']}"
