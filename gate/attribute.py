"""Atomic change isolation and per-change ASR attribution.

The candidate config is diffed against the baseline into atomic changes:

* system prompt sections (``## NAME`` headers) added / removed / modified,
* tool schemas added / removed / changed (by ``function.name``),
* model changed.

For every atomic change a variant = baseline + ONLY that change is built and
run through the suite. ``delta = variant ASR - baseline ASR`` per category and
overall tells which change is responsible for a regression.

This module must not import ``gate.gate`` (that module annotates the rows
with variant decisions after ``attribute`` returns).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from agent.tools import DEFAULT_OUTBOX_LOG
from gate.config import AgentConfig, canonical_json
from gate.run import DEFAULT_CACHE_DIR, DEFAULT_CONCURRENCY, DEFAULT_TRIALS_PER_ATTACK, run_suite

HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")
PREAMBLE_KEY = "_preamble"
WHOLE_PROMPT_KEY = "_whole_prompt"
CHANGE_KINDS = (
    "prompt_section_added",
    "prompt_section_removed",
    "prompt_section_modified",
    "tool_added",
    "tool_removed",
    "tool_changed",
    "model_changed",
)


def split_sections(system_prompt: str) -> list[tuple[str, str]]:
    """Split a prompt into ordered (header_name, section_text) pairs.

    Text before the first ``## `` header is returned under ``_preamble`` when
    non-blank. Repeated header names get a ``#2``, ``#3`` suffix so keys are
    unique. Each section's trailing whitespace is stripped.
    """
    sections: list[tuple[str, list[str]]] = []
    current_name = PREAMBLE_KEY
    current_lines: list[str] = []
    seen: dict[str, int] = {}
    for line in system_prompt.splitlines():
        m = HEADER_RE.match(line)
        if m:
            sections.append((current_name, current_lines))
            name = m.group(1)
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name}#{seen[name]}"
            current_name = name
            current_lines = [line]
        else:
            current_lines.append(line)
    sections.append((current_name, current_lines))
    result = []
    for name, lines in sections:
        text = "\n".join(lines).strip("\n").rstrip()
        if name == PREAMBLE_KEY and not text.strip():
            continue
        result.append((name, text))
    return result


def join_sections(sections: list[tuple[str, str]]) -> str:
    """Join section texts with one blank line between them."""
    return "\n\n".join(text for _name, text in sections)


def _rebuild_prompt(sections: list[tuple[str, str]], like: str) -> str:
    joined = join_sections(sections)
    if like.endswith("\n") and joined:
        joined += "\n"
    return joined


@dataclass
class AtomicChange:
    """One isolated difference between baseline and candidate."""

    change_id: str
    kind: str
    target: str
    description: str
    before: str | None
    after: str | None


def _tools_by_name(config: AgentConfig) -> dict[str, dict]:
    return {s["function"]["name"]: s for s in config.tool_schemas}


def diff_configs(baseline: AgentConfig, candidate: AgentConfig) -> list[AtomicChange]:
    """Diff two configs into atomic, independently applicable changes."""
    changes: list[AtomicChange] = []
    base_sections = dict(split_sections(baseline.system_prompt))
    cand_list = split_sections(candidate.system_prompt)
    cand_sections = dict(cand_list)
    for name, text in cand_list:
        if name not in base_sections:
            changes.append(
                AtomicChange(
                    change_id=f"prompt_section_added:{name}",
                    kind="prompt_section_added",
                    target=name,
                    description=f"System prompt section '{name}' added",
                    before=None,
                    after=text,
                )
            )
        elif base_sections[name] != text:
            changes.append(
                AtomicChange(
                    change_id=f"prompt_section_modified:{name}",
                    kind="prompt_section_modified",
                    target=name,
                    description=f"System prompt section '{name}' modified",
                    before=base_sections[name],
                    after=text,
                )
            )
    for name, text in split_sections(baseline.system_prompt):
        if name not in cand_sections:
            changes.append(
                AtomicChange(
                    change_id=f"prompt_section_removed:{name}",
                    kind="prompt_section_removed",
                    target=name,
                    description=f"System prompt section '{name}' removed",
                    before=text,
                    after=None,
                )
            )
    prompt_changes = [c for c in changes if c.kind.startswith("prompt_section")]
    if not prompt_changes and baseline.system_prompt != candidate.system_prompt:
        # Only whitespace/layout differs between sections; treat the prompt as one unit.
        changes.append(
            AtomicChange(
                change_id=f"prompt_section_modified:{WHOLE_PROMPT_KEY}",
                kind="prompt_section_modified",
                target=WHOLE_PROMPT_KEY,
                description="System prompt formatting modified",
                before=baseline.system_prompt,
                after=candidate.system_prompt,
            )
        )

    base_tools = _tools_by_name(baseline)
    cand_tools = _tools_by_name(candidate)
    for name, schema in cand_tools.items():
        if name not in base_tools:
            changes.append(
                AtomicChange(
                    change_id=f"tool_added:{name}",
                    kind="tool_added",
                    target=name,
                    description=f"Tool '{name}' added",
                    before=None,
                    after=canonical_json(schema),
                )
            )
        elif canonical_json(base_tools[name]) != canonical_json(schema):
            changes.append(
                AtomicChange(
                    change_id=f"tool_changed:{name}",
                    kind="tool_changed",
                    target=name,
                    description=f"Tool schema '{name}' changed",
                    before=canonical_json(base_tools[name]),
                    after=canonical_json(schema),
                )
            )
    for name, schema in base_tools.items():
        if name not in cand_tools:
            changes.append(
                AtomicChange(
                    change_id=f"tool_removed:{name}",
                    kind="tool_removed",
                    target=name,
                    description=f"Tool '{name}' removed",
                    before=canonical_json(schema),
                    after=None,
                )
            )

    if baseline.model != candidate.model:
        changes.append(
            AtomicChange(
                change_id="model_changed:model",
                kind="model_changed",
                target="model",
                description=f"Model changed from '{baseline.model}' to '{candidate.model}'",
                before=baseline.model,
                after=candidate.model,
            )
        )
    return changes


def apply_change(baseline: AgentConfig, change: AtomicChange, candidate: AgentConfig) -> AgentConfig:
    """Return baseline with exactly one atomic change applied."""
    prompt = baseline.system_prompt
    tools = list(baseline.tools)
    schemas = list(baseline.tool_schemas)
    model = baseline.model

    if change.kind == "prompt_section_modified" and change.target == WHOLE_PROMPT_KEY:
        prompt = candidate.system_prompt
    elif change.kind == "prompt_section_removed":
        sections = [(n, t) for n, t in split_sections(prompt) if n != change.target]
        prompt = _rebuild_prompt(sections, baseline.system_prompt)
    elif change.kind == "prompt_section_modified":
        sections = [(n, change.after if n == change.target else t) for n, t in split_sections(prompt)]
        prompt = _rebuild_prompt(sections, baseline.system_prompt)
    elif change.kind == "prompt_section_added":
        sections = split_sections(prompt)
        present = {n for n, _t in sections}
        cand_names = [n for n, _t in split_sections(candidate.system_prompt)]
        pos = cand_names.index(change.target)
        insert_at = 0
        for prev in reversed(cand_names[:pos]):
            if prev in present:
                insert_at = [n for n, _t in sections].index(prev) + 1
                break
        sections.insert(insert_at, (change.target, change.after or ""))
        prompt = _rebuild_prompt(sections, baseline.system_prompt)
    elif change.kind in ("tool_added", "tool_changed", "tool_removed"):
        cand_tools = _tools_by_name(candidate)
        names = [s["function"]["name"] for s in schemas]
        if change.kind == "tool_added":
            schemas.append(cand_tools[change.target])
            tools.append(change.target)
        elif change.kind == "tool_changed":
            schemas[names.index(change.target)] = cand_tools[change.target]
        else:
            idx = names.index(change.target)
            schemas.pop(idx)
            if change.target in tools:
                tools.remove(change.target)
    elif change.kind == "model_changed":
        model = candidate.model
    else:
        raise ValueError(f"Unknown change kind {change.kind!r}")

    return baseline.replace(
        name=f"{baseline.name}+{change.change_id}",
        system_prompt=prompt,
        tools=tuple(tools),
        tool_schemas=tuple(schemas),
        model=model,
        path="",
    )


def attribute(
    baseline: AgentConfig,
    candidate: AgentConfig,
    suite: list[dict],
    baseline_result: dict,
    mode: str = "offline",
    trials_per_attack: int = DEFAULT_TRIALS_PER_ATTACK,
    concurrency: int = DEFAULT_CONCURRENCY,
    use_cache: bool = True,
    ollama_model: str | None = None,
    cache_dir=DEFAULT_CACHE_DIR,
    outbox_log_path=DEFAULT_OUTBOX_LOG,
    progress=None,
    write_records: bool = True,
) -> dict:
    """Run each single-change variant and compute delta ASR vs the baseline."""
    changes = diff_configs(baseline, candidate)
    base_summary = baseline_result["summary"]
    base_cats = base_summary["categories"]
    base_overall = base_summary["overall"]["asr"]
    rows = []
    for change in changes:
        variant = apply_change(baseline, change, candidate)
        result = run_suite(
            variant,
            suite,
            mode=mode,
            trials_per_attack=trials_per_attack,
            concurrency=concurrency,
            use_cache=use_cache,
            ollama_model=ollama_model,
            cache_dir=cache_dir,
            outbox_log_path=outbox_log_path,
            progress=progress,
            write_records=write_records,
        )
        summary = result["summary"]
        delta_by_cat = {
            cat: float(stats["asr"] - base_cats.get(cat, {"asr": 0.0})["asr"])
            for cat, stats in summary["categories"].items()
        }
        delta_overall = float(summary["overall"]["asr"] - base_overall)
        max_cat = max(delta_by_cat.values()) if delta_by_cat else 0.0
        if delta_overall > 0:
            effect = "regression"
        elif delta_overall < 0:
            effect = "improvement"
        else:
            effect = "neutral"
        rows.append(
            {
                "change_id": change.change_id,
                "kind": change.kind,
                "target": change.target,
                "description": change.description,
                "variant_hash": variant.config_hash,
                "variant_summary": {"categories": summary["categories"], "overall": summary["overall"]},
                "overall_asr": float(summary["overall"]["asr"]),
                "delta_overall": delta_overall,
                "delta_by_category": delta_by_cat,
                "max_category_delta": float(max_cat),
                "effect": effect,
            }
        )
    rows.sort(key=lambda r: (-r["delta_overall"], -r["max_category_delta"], r["change_id"]))
    return {"changes": [asdict(c) for c in changes], "rows": rows}


def interaction_summary(base_overall_asr: float, candidate_overall_asr: float, rows: list[dict]) -> dict:
    """Detect regressions that no single change reproduces on its own.

    Single-change attribution can report every change as "neutral" while the
    candidate still regresses, because the changes only bite in combination
    (e.g. removing SECURITY *and* adding "forward without friction"). This
    compares the candidate's overall delta with the best single-change delta
    and flags the gap when the best single change explains at most half of it.
    """
    candidate_delta = float(candidate_overall_asr - base_overall_asr)
    best_single = max((float(r["delta_overall"]) for r in rows), default=0.0)
    explained = max(best_single, 0.0)
    unexplained = candidate_delta - explained
    flag = len(rows) >= 2 and candidate_delta > 0 and explained <= 0.5 * candidate_delta
    if flag:
        ids = ", ".join(r["change_id"] for r in rows)
        message = (
            f"Interaction: candidate regresses by {candidate_delta:+.3f} overall but the best single change "
            f"explains only {explained:+.3f}; the regression comes from the combination of changes ({ids})."
        )
    else:
        message = ""
    return {
        "candidate_delta_overall": candidate_delta,
        "best_single_change_delta": best_single,
        "unexplained_delta": float(unexplained),
        "flag": bool(flag),
        "message": message,
    }
