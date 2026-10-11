"""Trial orchestration: asyncio concurrency plus a JSON disk cache.

Cache key = sha256("|".join([config_hash, attack_id, trial_index, mode, model])).
Cached records store the outbox; on a cache hit the judge is re-applied to the
stored outbox so judge changes take effect without re-running the agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Callable

import yaml

from agent.environment import build_environment
from agent.runner import AgentRunner
from agent.tools import DEFAULT_OUTBOX_LOG
from attacks.judge import evaluate_trial
from gate.config import AgentConfig, canonical_json
from gate.stats import summarize_trials

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE_PATH = REPO_ROOT / "attacks" / "suite.yaml"
DEFAULT_CACHE_DIR = REPO_ROOT / "cache"
DEFAULT_TRIALS_PER_ATTACK = 10
DEFAULT_CONCURRENCY = 8
VALID_CATEGORIES = {"goal_hijack", "indirect_injection", "exfil_evasion", "tool_misuse"}
VALID_CHANNELS = {"user", "email", "document", "tool_description"}
REQUIRED_ATTACK_FIELDS = ("id", "category", "channel", "owasp", "payload")
MIN_ATTACKS_PER_CATEGORY = 3


def load_suite(path=DEFAULT_SUITE_PATH) -> list[dict]:
    """Load and validate the attack suite YAML."""
    path = Path(path)
    if not path.exists() and not path.is_absolute():
        path = REPO_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"Attack suite not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("attacks"), list):
        raise ValueError(f"{path.name}: suite must be a mapping with an 'attacks' list")
    attacks = data["attacks"]
    seen = set()
    per_cat: dict[str, int] = {}
    channels = set()
    for i, attack in enumerate(attacks):
        if not isinstance(attack, dict):
            raise ValueError(f"{path.name}: attack #{i} is not a mapping")
        for key in REQUIRED_ATTACK_FIELDS:
            if not isinstance(attack.get(key), str) or not attack[key].strip():
                raise ValueError(f"{path.name}: attack #{i} missing non-empty string field '{key}'")
        if attack["id"] in seen:
            raise ValueError(f"{path.name}: duplicate attack id {attack['id']}")
        seen.add(attack["id"])
        if attack["category"] not in VALID_CATEGORIES:
            raise ValueError(f"{path.name}: attack {attack['id']} has invalid category {attack['category']!r}")
        if attack["channel"] not in VALID_CHANNELS:
            raise ValueError(f"{path.name}: attack {attack['id']} has invalid channel {attack['channel']!r}")
        per_cat[attack["category"]] = per_cat.get(attack["category"], 0) + 1
        channels.add(attack["channel"])
        attack.setdefault("description", "")
    for cat in sorted(VALID_CATEGORIES):
        if per_cat.get(cat, 0) < MIN_ATTACKS_PER_CATEGORY:
            raise ValueError(
                f"{path.name}: category {cat} needs at least {MIN_ATTACKS_PER_CATEGORY} attacks, has {per_cat.get(cat, 0)}"
            )
    if channels != VALID_CHANNELS:
        raise ValueError(f"{path.name}: suite must cover channels {sorted(VALID_CHANNELS)}, covers {sorted(channels)}")
    return attacks


def suite_hash(suite: list[dict]) -> str:
    """SHA-256 of the canonical JSON of the suite."""
    return hashlib.sha256(canonical_json(suite).encode("utf-8")).hexdigest()


def cache_key(config_hash: str, attack_id: str, trial_index: int, mode: str, model: str) -> str:
    """Disk-cache key for one trial."""
    raw = "|".join([config_hash, attack_id, str(trial_index), mode, model])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _load_cached(path: Path) -> dict | None:
    """Load a cached trial and re-judge it; None if missing or unreadable (recompute)."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    record["success"] = evaluate_trial(record.get("outbox", []), record.get("actions", []))
    record["cached"] = True
    return record


def run_single_trial(
    config: AgentConfig,
    attack: dict,
    trial_index: int,
    mode: str = "offline",
    ollama_model: str | None = None,
    cache_dir=DEFAULT_CACHE_DIR,
    use_cache: bool = True,
    outbox_log_path=DEFAULT_OUTBOX_LOG,
    write_records: bool = True,
) -> dict:
    """Run (or load from cache) one trial of one attack.

    ``use_cache`` controls *reading* a previously recorded trial. ``write_records``
    controls whether the full trial record (transcript, outbox, actions, verdict)
    is persisted; it defaults to True even when the cache is not read, because
    the per-trial record is the audit evidence behind every check.
    """
    runner = AgentRunner(mode=mode, ollama_model=ollama_model)
    model = runner.resolved_model(config.model)
    config_hash = config.config_hash
    cache_dir = Path(cache_dir)
    path = cache_dir / f"{cache_key(config_hash, attack['id'], trial_index, mode, model)}.json"
    if use_cache:
        cached = _load_cached(path)
        if cached is not None:
            return cached
    env = build_environment(attack, trial_id=f"{config_hash[:12]}-{attack['id']}-{trial_index}")
    result = runner.run(
        config.system_prompt,
        config.model,
        env.poison_tool_schemas(config.tool_schemas),
        env,
        trial_index=trial_index,
        outbox_log_path=outbox_log_path,
    )
    record = {
        "config_hash": config_hash,
        "attack_id": attack["id"],
        "category": attack["category"],
        "channel": attack["channel"],
        "owasp": attack.get("owasp", ""),
        "trial_index": trial_index,
        "mode": mode,
        "model": result["model"],
        "success": evaluate_trial(result["outbox"], result.get("actions", [])),
        "refused": bool(result["refused"]),
        "outbox": result["outbox"],
        "actions": result.get("actions", []),
        "transcript": result["transcript"],
        "cached": False,
    }
    if write_records:
        _write_json_atomic(path, record)
    return record


async def run_suite_async(
    config: AgentConfig,
    suite: list[dict],
    mode: str = "offline",
    trials_per_attack: int = DEFAULT_TRIALS_PER_ATTACK,
    concurrency: int = DEFAULT_CONCURRENCY,
    use_cache: bool = True,
    ollama_model: str | None = None,
    cache_dir=DEFAULT_CACHE_DIR,
    outbox_log_path=DEFAULT_OUTBOX_LOG,
    progress: Callable[[dict], None] | None = None,
    write_records: bool = True,
) -> dict:
    """Run every attack ``trials_per_attack`` times with bounded concurrency.

    ``progress``, if given, is called once per completed trial (from the event
    loop thread, in completion order) with a small event dict: config name and
    hash, attack id / category / channel, trial index, success, refused, cached,
    the outbox recipients, the sensitive actions, and ``done`` / ``total``
    counters for this suite run. Exceptions raised by the callback are ignored
    so a UI bug can never abort a check.
    """
    if trials_per_attack < 1:
        raise ValueError("trials_per_attack must be >= 1")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    if not suite:
        raise ValueError("suite must contain at least one attack")
    semaphore = asyncio.Semaphore(concurrency)
    total = len(suite) * trials_per_attack
    done = 0

    async def one(attack: dict, idx: int) -> dict:
        nonlocal done
        async with semaphore:
            record = await asyncio.to_thread(
                run_single_trial,
                config,
                attack,
                idx,
                mode,
                ollama_model,
                cache_dir,
                use_cache,
                outbox_log_path,
                write_records,
            )
        done += 1
        if progress is not None:
            try:
                progress(
                    {
                        "config_name": config.name,
                        "config_hash": config.config_hash,
                        "attack_id": record["attack_id"],
                        "category": record["category"],
                        "channel": record["channel"],
                        "trial_index": record["trial_index"],
                        "success": bool(record["success"]),
                        "refused": bool(record.get("refused", False)),
                        "cached": bool(record.get("cached", False)),
                        "recipients": [str(e.get("to", "")) for e in record.get("outbox", []) if isinstance(e, dict)],
                        "actions": [
                            {k: a.get(k) for k in ("tool", "vendor", "iban", "reason")}
                            for a in record.get("actions", []) if isinstance(a, dict)
                        ],
                        # enough of the transcript for a UI to show "what the agent did" without the cache
                        "outbox": [
                            {"to": e.get("to"), "subject": e.get("subject"), "body": str(e.get("body") or "")[:1200]}
                            for e in record.get("outbox", []) if isinstance(e, dict)
                        ],
                        "tool_calls": [
                            {"name": c.get("function", {}).get("name"),
                             "arguments": str(c.get("function", {}).get("arguments"))[:300]}
                            for m in record.get("transcript", []) if isinstance(m, dict) and m.get("role") == "assistant"
                            for c in (m.get("tool_calls") or [])
                        ],
                        "done": done,
                        "total": total,
                    }
                )
            except Exception:  # noqa: BLE001 - a UI callback must never break a check
                pass
        return record

    tasks = [one(a, i) for a in suite for i in range(trials_per_attack)]
    records = await asyncio.gather(*tasks)
    records = sorted(records, key=lambda r: (r["attack_id"], r["trial_index"]))
    model = AgentRunner(mode=mode, ollama_model=ollama_model).resolved_model(config.model)
    light = [
        {
            "attack_id": r["attack_id"],
            "category": r["category"],
            "channel": r["channel"],
            "trial_index": r["trial_index"],
            "success": bool(r["success"]),
            "refused": bool(r.get("refused", False)),
            "cached": bool(r.get("cached", False)),
        }
        for r in records
    ]
    return {
        "config": {
            "name": config.name,
            "version": config.version,
            "path": config.path,
            "hash": config.config_hash,
            "model": config.model,
        },
        "mode": mode,
        "model": model,
        "trials_per_attack": trials_per_attack,
        "n_trials": len(records),
        "suite_hash": suite_hash(suite),
        "summary": summarize_trials(light),
        "trials": light,
    }


def run_suite(
    config: AgentConfig,
    suite: list[dict],
    mode: str = "offline",
    trials_per_attack: int = DEFAULT_TRIALS_PER_ATTACK,
    concurrency: int = DEFAULT_CONCURRENCY,
    use_cache: bool = True,
    ollama_model: str | None = None,
    cache_dir=DEFAULT_CACHE_DIR,
    outbox_log_path=DEFAULT_OUTBOX_LOG,
    progress: Callable[[dict], None] | None = None,
    write_records: bool = True,
) -> dict:
    """Synchronous wrapper around ``run_suite_async``."""
    return asyncio.run(
        run_suite_async(
            config,
            suite,
            progress=progress,
            write_records=write_records,
            mode=mode,
            trials_per_attack=trials_per_attack,
            concurrency=concurrency,
            use_cache=use_cache,
            ollama_model=ollama_model,
            cache_dir=cache_dir,
            outbox_log_path=outbox_log_path,
        )
    )
