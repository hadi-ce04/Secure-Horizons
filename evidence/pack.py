"""Evidence pack generation (Markdown + accessible HTML) logged to the ledger.

A pack is written to ``evidence/packs/<UTC timestamp>_<candidate hash[:12]>/``
and contains ``pack.md`` and ``pack.html``. The SHA-256 of each file is
recorded in a ``pack_generated`` ledger entry.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jinja2 import Environment as JinjaEnvironment

from agent.tools import DEFAULT_OUTBOX_LOG
from gate.config import load_config
from gate.gate import DEFAULT_BASELINE, DEFAULT_LATEST_PATH, load_latest_check, run_check
from gate.run import DEFAULT_CACHE_DIR, DEFAULT_TRIALS_PER_ATTACK
from ledger.ledger import Ledger, get_default_ledger, utc_now_iso

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKS_DIR = REPO_ROOT / "evidence" / "packs"
MAPPING_PATH = REPO_ROOT / "evidence" / "mapping.yaml"
BADGE_COLORS = {
    "ALLOW": {"fg": "#1b5e20", "bg": "#e8f5e9"},
    "BLOCK": {"fg": "#b71c1c", "bg": "#ffebee"},
    "REVIEW": {"fg": "#6d4c00", "bg": "#fff8e1"},
}

MD_TEMPLATE = """\
# AgentGate Evidence Pack

- Generated at: {{ generated_at }}
- Decision: **{{ decision.decision }}** (threshold T = {{ "%.2f"|format(decision.threshold) }})
- Check generated at: {{ check.generated_at }}

## Configurations

| Role | Name | Version | Path | Config hash (SHA-256) |
|---|---|---|---|---|
| Baseline | {{ check.baseline.name }} | {{ check.baseline.version }} | {{ check.baseline.path }} | `{{ check.baseline.hash }}` |
| Candidate | {{ check.candidate.name }} | {{ check.candidate.version }} | {{ check.candidate.path }} | `{{ check.candidate.hash }}` |

## Run parameters

- Mode: {{ check.mode }}
- Model: {{ check.model }}
- Trials per attack: {{ check.trials_per_attack }}
- Baseline trials: {{ check.baseline.n_trials }}
- Candidate trials: {{ check.candidate.n_trials }}
- Attack suite hash: `{{ check.suite_hash }}`

## Attack success rate (Wilson 95% CI)

{% for side in sides %}
### {{ side.label }}: {{ side.data.name }}

| Category | n | Successes | ASR | CI lower | CI upper | Agreement |
|---|---|---|---|---|---|---|
{% for cat, s in side.data.summary.categories.items() %}
| {{ cat }} | {{ s.n }} | {{ s.successes }} | {{ "%.3f"|format(s.asr) }} | {{ "%.3f"|format(s.ci_lower) }} | {{ "%.3f"|format(s.ci_upper) }} | {{ "%.3f"|format(s.agreement) }} |
{% endfor %}
| overall | {{ side.data.summary.overall.n }} | {{ side.data.summary.overall.successes }} | {{ "%.3f"|format(side.data.summary.overall.asr) }} | {{ "%.3f"|format(side.data.summary.overall.ci_lower) }} | {{ "%.3f"|format(side.data.summary.overall.ci_upper) }} | {{ "%.3f"|format(side.data.summary.overall.agreement) }} |

{% endfor %}
## Gate decision: {{ decision.decision }}

{% for r in decision.reasons %}
- {{ r }}
{% endfor %}

## Attribution breakdown

{% if attribution_rows %}
| Change | Kind | Delta overall ASR | Max category delta | Effect | Variant decision |
|---|---|---|---|---|---|
{% for row in attribution_rows %}
| {{ row.change_id }} | {{ row.kind }} | {{ "%+.3f"|format(row.delta_overall) }} | {{ "%+.3f"|format(row.max_category_delta) }} | {{ row.effect }} | {{ row.variant_decision }} |
{% endfor %}
{% else %}
No changes between baseline and candidate.
{% endif %}

## Override

{% if override %}
- Actor: {{ override.actor }}
- Reason: {{ override.reason }}
- Forced: {{ override.forced }}
- Override entry hash: `{{ override.entry_hash }}`
{% else %}
None
{% endif %}

## Compliance mapping

| Control | Name | {{ frameworks.eu_ai_act }} | {{ frameworks.nist_ai_rmf }} | {{ frameworks.iso_42001 }} |
|---|---|---|---|---|
{% for c in controls %}
| {{ c.id }} | {{ c.name }} | {{ c.mappings.eu_ai_act|join("; ") }} | {{ c.mappings.nist_ai_rmf|join("; ") }} | {{ c.mappings.iso_42001|join("; ") }} |
{% endfor %}

## Ledger reference

- Ledger head hash at generation: `{{ ledger_head }}`
- Check entry hash: `{{ check.ledger.check_entry_hash }}`
- Gate decision entry hash: `{{ check.ledger.decision_entry_hash }}` (index {{ check.ledger.decision_index }})

## Verification

Run `python ledger/verify.py` from the repository root. It checks index continuity, hash chaining,
recomputed entry hashes, and Ed25519 signatures against `.keys/public.pem`, and exits 0 when the
ledger is intact.
"""

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentGate Evidence Pack - {{ check.candidate.name }} - {{ decision.decision }}</title>
<style>
body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: #1a1a1a; background: #ffffff; margin: 2rem; line-height: 1.5; }
table { border-collapse: collapse; margin: 1rem 0; }
caption { text-align: left; font-weight: 600; padding: 0.25rem 0; }
th, td { border: 1px solid #5f5f5f; padding: 0.35rem 0.6rem; text-align: left; vertical-align: top; }
thead th { background: #eeeeee; }
code { font-family: Consolas, "Courier New", monospace; word-break: break-all; }
.badge { display: inline-block; padding: 0.3rem 0.8rem; border: 2px solid {{ badge.fg }}; border-radius: 0.3rem; color: {{ badge.fg }}; background: {{ badge.bg }}; font-weight: 700; }
</style>
</head>
<body>
<main>
<h1>AgentGate Evidence Pack</h1>
<p>Generated at {{ generated_at }}. Check generated at {{ check.generated_at }}.</p>
<p><span class="badge" role="status">Decision: {{ decision.decision }}</span> (threshold T = {{ "%.2f"|format(decision.threshold) }})</p>

<h2>Configurations</h2>
<table>
<caption>Baseline and candidate configurations</caption>
<thead><tr><th scope="col">Role</th><th scope="col">Name</th><th scope="col">Version</th><th scope="col">Path</th><th scope="col">Config hash (SHA-256)</th></tr></thead>
<tbody>
<tr><th scope="row">Baseline</th><td>{{ check.baseline.name }}</td><td>{{ check.baseline.version }}</td><td>{{ check.baseline.path }}</td><td><code>{{ check.baseline.hash }}</code></td></tr>
<tr><th scope="row">Candidate</th><td>{{ check.candidate.name }}</td><td>{{ check.candidate.version }}</td><td>{{ check.candidate.path }}</td><td><code>{{ check.candidate.hash }}</code></td></tr>
</tbody>
</table>

<h2>Run parameters</h2>
<ul>
<li>Mode: {{ check.mode }}</li>
<li>Model: {{ check.model }}</li>
<li>Trials per attack: {{ check.trials_per_attack }}</li>
<li>Baseline trials: {{ check.baseline.n_trials }}</li>
<li>Candidate trials: {{ check.candidate.n_trials }}</li>
<li>Attack suite hash: <code>{{ check.suite_hash }}</code></li>
</ul>

<h2>Attack success rate (Wilson 95% CI)</h2>
{% for side in sides %}
<table>
<caption>{{ side.label }}: {{ side.data.name }}</caption>
<thead><tr><th scope="col">Category</th><th scope="col">n</th><th scope="col">Successes</th><th scope="col">ASR</th><th scope="col">CI lower</th><th scope="col">CI upper</th><th scope="col">Agreement</th></tr></thead>
<tbody>
{% for cat, s in side.data.summary.categories.items() %}
<tr><th scope="row">{{ cat }}</th><td>{{ s.n }}</td><td>{{ s.successes }}</td><td>{{ "%.3f"|format(s.asr) }}</td><td>{{ "%.3f"|format(s.ci_lower) }}</td><td>{{ "%.3f"|format(s.ci_upper) }}</td><td>{{ "%.3f"|format(s.agreement) }}</td></tr>
{% endfor %}
{% set o = side.data.summary.overall %}
<tr><th scope="row">overall</th><td>{{ o.n }}</td><td>{{ o.successes }}</td><td>{{ "%.3f"|format(o.asr) }}</td><td>{{ "%.3f"|format(o.ci_lower) }}</td><td>{{ "%.3f"|format(o.ci_upper) }}</td><td>{{ "%.3f"|format(o.agreement) }}</td></tr>
</tbody>
</table>
{% endfor %}

<h2>Gate decision: {{ decision.decision }}</h2>
<ul>
{% for r in decision.reasons %}
<li>{{ r }}</li>
{% endfor %}
</ul>

<h2>Attribution breakdown</h2>
{% if attribution_rows %}
<table>
<caption>Per-change attribution (variant = baseline + one change)</caption>
<thead><tr><th scope="col">Change</th><th scope="col">Kind</th><th scope="col">Delta overall ASR</th><th scope="col">Max category delta</th><th scope="col">Effect</th><th scope="col">Variant decision</th></tr></thead>
<tbody>
{% for row in attribution_rows %}
<tr><th scope="row">{{ row.change_id }}</th><td>{{ row.kind }}</td><td>{{ "%+.3f"|format(row.delta_overall) }}</td><td>{{ "%+.3f"|format(row.max_category_delta) }}</td><td>{{ row.effect }}</td><td>{{ row.variant_decision }}</td></tr>
{% endfor %}
</tbody>
</table>
{% else %}
<p>No changes between baseline and candidate.</p>
{% endif %}

<h2>Override</h2>
{% if override %}
<ul>
<li>Actor: {{ override.actor }}</li>
<li>Reason: {{ override.reason }}</li>
<li>Forced: {{ override.forced }}</li>
<li>Override entry hash: <code>{{ override.entry_hash }}</code></li>
</ul>
{% else %}
<p>None</p>
{% endif %}

<h2>Compliance mapping</h2>
<table>
<caption>AgentGate controls mapped to regulatory frameworks</caption>
<thead><tr><th scope="col">Control</th><th scope="col">Name</th><th scope="col">{{ frameworks.eu_ai_act }}</th><th scope="col">{{ frameworks.nist_ai_rmf }}</th><th scope="col">{{ frameworks.iso_42001 }}</th></tr></thead>
<tbody>
{% for c in controls %}
<tr><th scope="row">{{ c.id }}</th><td>{{ c.name }}</td><td>{{ c.mappings.eu_ai_act|join("; ") }}</td><td>{{ c.mappings.nist_ai_rmf|join("; ") }}</td><td>{{ c.mappings.iso_42001|join("; ") }}</td></tr>
{% endfor %}
</tbody>
</table>

<h2>Ledger reference</h2>
<ul>
<li>Ledger head hash at generation: <code>{{ ledger_head }}</code></li>
<li>Check entry hash: <code>{{ check.ledger.check_entry_hash }}</code></li>
<li>Gate decision entry hash: <code>{{ check.ledger.decision_entry_hash }}</code> (index {{ check.ledger.decision_index }})</li>
</ul>

<h2>Verification</h2>
<p>Run <code>python ledger/verify.py</code> from the repository root. It checks index continuity, hash chaining,
recomputed entry hashes, and Ed25519 signatures against <code>.keys/public.pem</code>, and exits 0 when the
ledger is intact.</p>
</main>
</body>
</html>
"""

_MD_ENV = JinjaEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
_HTML_ENV = JinjaEnvironment(autoescape=True, trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)


def load_mapping(path=MAPPING_PATH) -> dict:
    """Load the compliance mapping YAML."""
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "frameworks" not in data or "controls" not in data:
        raise ValueError(f"{Path(path).name}: mapping must define 'frameworks' and 'controls'")
    return data


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _needs_fresh_check(latest: dict | None, candidate_path, baseline_path) -> bool:
    if latest is None:
        return True
    if candidate_path is not None and load_config(candidate_path).config_hash != latest["candidate"]["hash"]:
        return True
    if baseline_path is not None and load_config(baseline_path).config_hash != latest["baseline"]["hash"]:
        return True
    return False


def render_pack(check: dict, mapping: dict, ledger_head: str | None, generated_at: str) -> tuple[str, str]:
    """Render (markdown, html) for a check result."""
    decision = check["decision"]
    context = {
        "generated_at": generated_at,
        "check": check,
        "decision": decision,
        "badge": BADGE_COLORS.get(decision["decision"], BADGE_COLORS["REVIEW"]),
        "sides": [
            {"label": "Baseline", "data": check["baseline"]},
            {"label": "Candidate", "data": check["candidate"]},
        ],
        "attribution_rows": check.get("attribution", {}).get("rows", []),
        "override": check.get("override"),
        "frameworks": mapping["frameworks"],
        "controls": mapping["controls"],
        "ledger_head": ledger_head or "(empty ledger)",
    }
    md = _MD_ENV.from_string(MD_TEMPLATE).render(**context)
    html = _HTML_ENV.from_string(HTML_TEMPLATE).render(**context)
    return md, html


def generate_pack(
    candidate_path=None,
    baseline_path=None,
    mode: str = "offline",
    trials_per_attack: int = DEFAULT_TRIALS_PER_ATTACK,
    ledger: Ledger | None = None,
    latest_path=DEFAULT_LATEST_PATH,
    packs_dir=PACKS_DIR,
    cache_dir=DEFAULT_CACHE_DIR,
    outbox_log_path=DEFAULT_OUTBOX_LOG,
) -> dict:
    """Generate an evidence pack from the latest check (running one if needed)."""
    ledger = ledger or get_default_ledger()
    latest = load_latest_check(latest_path)
    if _needs_fresh_check(latest, candidate_path, baseline_path):
        latest = run_check(
            candidate_path or DEFAULT_BASELINE,
            baseline_path or DEFAULT_BASELINE,
            mode=mode,
            trials_per_attack=trials_per_attack,
            ledger=ledger,
            cache_dir=cache_dir,
            latest_path=latest_path,
            outbox_log_path=outbox_log_path,
        )
    mapping = load_mapping()
    ledger_head = ledger.head_hash()
    generated_at = utc_now_iso()

    packs_dir = Path(packs_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"{stamp}_{latest['candidate']['hash'][:12]}"
    pack_dir = packs_dir / base_name
    suffix = 1
    while pack_dir.exists():
        suffix += 1
        pack_dir = packs_dir / f"{base_name}_{suffix}"
    pack_dir.mkdir(parents=True)

    md, html = render_pack(latest, mapping, ledger_head, generated_at)
    md_path = pack_dir / "pack.md"
    html_path = pack_dir / "pack.html"
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(md)
    with open(html_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    shas = {"pack.md": _sha256_file(md_path), "pack.html": _sha256_file(html_path)}

    entry = ledger.append(
        "pack_generated",
        {
            "pack_dir": _display_path(pack_dir),
            "files": shas,
            "candidate_hash": latest["candidate"]["hash"],
            "baseline_hash": latest["baseline"]["hash"],
            "decision": latest["decision"]["decision"],
            "decision_entry_hash": latest["ledger"]["decision_entry_hash"],
            "ledger_head_at_generation": ledger_head,
        },
    )
    return {
        "pack_dir": str(pack_dir),
        "md_path": str(md_path),
        "html_path": str(html_path),
        "sha256": shas,
        "ledger_entry_hash": entry["entry_hash"],
        "decision": latest["decision"]["decision"],
    }


def list_packs(packs_dir=PACKS_DIR) -> list[Path]:
    """Return pack directories containing pack.md, newest first."""
    packs_dir = Path(packs_dir)
    if not packs_dir.exists():
        return []
    dirs = [p for p in packs_dir.iterdir() if p.is_dir() and (p / "pack.md").exists()]
    return sorted(dirs, key=lambda p: ((p / "pack.md").stat().st_mtime, p.name), reverse=True)
