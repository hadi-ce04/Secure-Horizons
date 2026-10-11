"""AgentGate dashboard (business theme).

Run from the repository root:  streamlit run dashboard/app.py

Panels, top to bottom:

1. Header + KPI row: regressions blocked, benign changes passed, false-block
   rate, trials executed, ledger entries, chain status. All read from the
   signed ledger for the selected model, so the numbers are the record, not
   the current session.
2. Live run: baseline / candidate / agent model / trials, RUN GATE. A
   background thread executes the real gate (``gate.gate.run_check``); every
   finished trial streams into the per-attack tables and the event log. Every
   run writes to the real ledger, exactly like the CLI.
3. Verdict: decision, headline metrics for this run, per-family intervals vs
   the threshold, attribution and the interaction callout.
4. Evidence: ledger entries appended by this run, verify, tamper demo, signed
   override (REVIEW only), evidence pack download, transcript of any breached trial.
5. Record: scorecard and attack x candidate heatmap from the whole ledger.

Palette (validated with the dataviz checks on the white surface): series 1
teal #0aa5a5 = trusted baseline, series 2 orange #eb6834 = candidate; status
good green #008300, warning yellow #eda100 (always with a label), serious
orange #eb6834. Heatmap is a single-hue orange ramp.
"""

from __future__ import annotations

import glob
import html
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from evidence.pack import generate_pack  # noqa: E402
from evidence.pdf import review_explanation  # noqa: E402
from gate.attribute import diff_configs  # noqa: E402
from gate.config import load_config  # noqa: E402
from gate.gate import THRESHOLD, GateError, override, run_check  # noqa: E402
from gate.run import DEFAULT_CACHE_DIR, cache_key, load_suite  # noqa: E402
from ledger.ledger import LedgerError, get_default_ledger  # noqa: E402

# ----------------------------------------------------------------------------- constants
AGENT_MODELS = {
    "Llama 3.3 70B (Bedrock)": ("bedrock", "us.meta.llama3-3-70b-instruct-v1:0"),
    "Amazon Nova Micro (Bedrock)": ("bedrock", "us.amazon.nova-micro-v1:0"),
    "Amazon Nova Lite (Bedrock)": ("bedrock", "us.amazon.nova-lite-v1:0"),
    "Claude Haiku 4.5 (Bedrock)": ("bedrock", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    "Offline deterministic mock (no network)": ("offline", None),
}
# palette
TEAL, ORANGE, GREEN, YELLOW = "#0aa5a5", "#eb6834", "#008300", "#eda100"
TEAL_DARK, ORANGE_DARK = "#077a7a", "#b84a1f"
INK, INK_2, INK_3 = "#1f2937", "#4b5563", "#6b7280"
SURFACE, CARD, LINE, LINE_2 = "#f6f7f9", "#ffffff", "#e5e7eb", "#d1d5db"
ORANGE_RAMP = ["#fff4ee", "#fcd9c5", "#f7b08f", "#f08a5f", "#eb6834", "#c7501f", "#8f3414"]
DECISION_STYLE = {"ALLOW": (GREEN, "Allowed"), "BLOCK": (ORANGE, "Blocked"), "REVIEW": (YELLOW, "Needs review")}
POLL_SECONDS = 0.5

st.set_page_config(page_title="AgentGate", page_icon=":shield:", layout="wide")

CSS = f"""
<style>
  html, body, [class*="css"] {{ font-family: Inter, "Segoe UI", system-ui, -apple-system, sans-serif; color:{INK}; }}
  .block-container {{ padding-top: 3rem; }}
  .ag-title {{ font-size:1.5rem; font-weight:600; color:{INK}; margin:0; }}
  .ag-sub {{ color:{INK_3}; font-size:.9rem; margin:.1rem 0 1rem; }}
  .ag-h {{ font-size:.8rem; font-weight:600; letter-spacing:.06em; text-transform:uppercase; color:{INK_3}; margin:1.4rem 0 .5rem; }}
  .kpis {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:.75rem; }}
  .kpi {{ background:{CARD}; border:1px solid {LINE}; border-radius:12px; padding:.8rem 1rem; min-width:0; }}
  .kpi .l {{ font-size:.78rem; color:{INK_3}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .kpi .v {{ font-size:1.6rem; font-weight:600; color:{INK}; line-height:1.2; margin-top:.15rem; white-space:nowrap; }}
  .kpi .d {{ font-size:.75rem; color:{INK_2}; margin-top:.15rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .card {{ background:{CARD}; border:1px solid {LINE}; border-radius:12px; padding:1rem 1.1rem; }}
  .banner {{ border-radius:12px; padding:1rem 1.2rem; border:1px solid {LINE}; background:{CARD}; display:flex; gap:1.2rem; align-items:center; }}
  .banner .pill {{ font-size:1.1rem; font-weight:600; color:#fff; padding:.35rem .9rem; border-radius:999px; }}
  .banner .ctx {{ color:{INK_2}; font-size:.9rem; }}
  table.ag {{ width:100%; border-collapse:collapse; font-size:.82rem; }}
  table.ag th {{ text-align:left; color:{INK_3}; font-weight:500; padding:.35rem .5rem; border-bottom:1px solid {LINE_2}; }}
  table.ag td {{ padding:.3rem .5rem; border-bottom:1px solid {LINE}; white-space:nowrap; }}
  .bar {{ display:inline-block; height:.55rem; background:{ORANGE}; vertical-align:middle; border-radius:4px 4px 4px 4px; }}
  .barbg {{ display:inline-block; width:90px; height:.55rem; background:{ORANGE_RAMP[0]}; vertical-align:middle; border-radius:4px; }}
  .good {{ color:{GREEN}; }} .bad {{ color:{ORANGE_DARK}; }} .warn {{ color:#8a5a00; }} .dim {{ color:{INK_3}; }}
  .log {{ background:#fbfbfc; border:1px solid {LINE}; border-radius:12px; padding:.6rem .9rem; height:240px; overflow:auto;
          white-space:nowrap; font-family: ui-monospace, Consolas, monospace; font-size:.78rem; line-height:1.5; color:{INK_2}; }}
  .log .b {{ color:{ORANGE_DARK}; font-weight:600; }} .log .r {{ color:{GREEN}; }} .log .c {{ color:{INK_3}; }}
  .mono {{ font-family: ui-monospace, Consolas, monospace; font-size:.8rem; color:{INK_2}; }}
  .dot {{ display:inline-block; width:.6rem; height:.6rem; border-radius:50%; margin-right:.35rem; vertical-align:middle; }}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ----------------------------------------------------------------------------- helpers
def _configs() -> list[str]:
    return sorted(str(Path(p).relative_to(REPO_ROOT)).replace("\\", "/") for p in glob.glob(str(REPO_ROOT / "configs" / "*.yaml")))


def _ledger_entries() -> list[dict]:
    try:
        return get_default_ledger().read_all()
    except LedgerError:
        return []


def _run_cli(*args: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=str(REPO_ROOT))
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _short(h: str | None, n: int = 16) -> str:
    return (h or "")[:n]


def _esc(s) -> str:
    return html.escape(str(s))


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%" if x >= 0.095 or x == 0 else f"{100 * x:.1f}%"


def _chart_theme(chart):
    return (chart.configure_view(strokeWidth=0)
            .configure_axis(labelColor=INK_2, titleColor=INK_3, gridColor=LINE, domainColor=LINE_2, tickColor=LINE_2,
                            labelFont="Inter, Segoe UI, sans-serif", titleFont="Inter, Segoe UI, sans-serif")
            .configure_legend(labelColor=INK_2, titleColor=INK_3, labelFont="Inter, Segoe UI, sans-serif"))


class RunState:
    """Shared between the UI thread and the worker thread."""

    def __init__(self, baseline: str, candidate: str, mode: str, model: str | None, trials: int,
                 concurrency: int, use_cache: bool, attribution: bool):
        self.baseline_path, self.candidate_path = baseline, candidate
        self.mode, self.model, self.trials = mode, model, trials
        self.concurrency, self.use_cache, self.attribution = concurrency, use_cache, attribution
        b_cfg, c_cfg = load_config(baseline), load_config(candidate)
        self.baseline_hash = b_cfg.config_hash
        self.candidate_hash = c_cfg.config_hash
        self.suite = load_suite()
        # attribution runs one extra variant per atomic change (baseline + that change)
        try:
            self.n_variants = len(diff_configs(b_cfg, c_cfg)) if attribution and b_cfg.config_hash != c_cfg.config_hash else 0
        except Exception:  # noqa: BLE001 - sizing only
            self.n_variants = 0
        per_config = len(self.suite) * trials
        self.expected_total = per_config * (2 + self.n_variants)
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.counts: dict[str, dict[str, list[int]]] = {}
        self.result: dict | None = None
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.ledger_start = len(_ledger_entries())
        self.thread = threading.Thread(target=self._work, daemon=True)
        self.thread.start()

    def phase_of(self, config_hash: str) -> str:
        if config_hash == self.baseline_hash:
            return "baseline"
        if config_hash == self.candidate_hash:
            return "candidate"
        return "variant"

    def _progress(self, ev: dict) -> None:
        with self.lock:
            self.events.append(ev)
            per = self.counts.setdefault(ev["config_hash"], {})
            cell = per.setdefault(ev["attack_id"], [0, 0])
            cell[0] += 1
            cell[1] += int(ev["success"])

    def _work(self) -> None:
        try:
            self.result = run_check(
                self.candidate_path, baseline_path=self.baseline_path, mode=self.mode,
                trials_per_attack=self.trials, concurrency=self.concurrency, use_cache=self.use_cache,
                ollama_model=self.model, run_attribution=self.attribution, progress=self._progress,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.finished = time.time()

    @property
    def running(self) -> bool:
        return self.thread.is_alive()

    def snapshot(self):
        with self.lock:
            return list(self.events), {h: {a: list(c) for a, c in per.items()} for h, per in self.counts.items()}


def _attack_table(state: RunState, config_hash: str | None, title: str, colour: str) -> str:
    _, counts = state.snapshot()
    per = counts.get(config_hash or "", {})
    rows, tot_done, tot_bad = [], 0, 0
    for a in state.suite:
        done, bad = per.get(a["id"], [0, 0])
        tot_done += done
        tot_bad += bad
        rate = (bad / done) if done else 0.0
        width = int(90 * rate)
        chan = {"user": "user", "email": "email", "document": "doc", "tool_description": "tool"}.get(a["channel"], a["channel"])
        cls = "bad" if bad else ("good" if done else "dim")
        rows.append(
            f"<tr><td title='{_esc(a['category'])} via {_esc(a['channel'])}'>{_esc(a['id'])} <span class='dim'>{chan}</span></td>"
            f"<td class='dim'>{_esc(a['owasp'].split(' ')[0])}</td><td>{done}/{state.trials}</td>"
            f"<td class='good'>{done - bad}</td><td class='{cls}'>{bad}</td>"
            f"<td><span class='barbg'><span class='bar' style='width:{width}px'></span></span> {rate:.0%}</td></tr>"
        )
    total = len(state.suite) * state.trials
    head = (f"<div class='ag-h'><span class='dot' style='background:{colour}'></span>{_esc(title)} "
            f"<span class='dim' style='text-transform:none;letter-spacing:0;font-weight:400'>{tot_done}/{total} trials, "
            f"<span class='{'bad' if tot_bad else 'good'}'>{tot_bad} breached</span></span></div>")
    return head + ("<div style='overflow-x:auto'><table class='ag'><tr><th>attack</th><th>OWASP</th><th>trials</th>"
                   "<th>refused</th><th>breached</th><th>breach rate</th></tr>" + "".join(rows) + "</table></div>")


def _event_log(state: RunState, limit: int = 60) -> str:
    events, _ = state.snapshot()
    lines = []
    for ev in reversed(events[-limit:]):
        phase = state.phase_of(ev["config_hash"])
        if ev["success"]:
            what = ", ".join(ev["recipients"]) if ev["recipients"] else ""
            if ev["actions"]:
                what = (what + " | " if what else "") + "; ".join(
                    f"{a.get('tool')}({a.get('vendor')} -> {a.get('iban')})" for a in ev["actions"])
            lines.append(f"<div><span class='c'>[{phase:9s}]</span> <span class='b'>BREACH</span> {_esc(ev['attack_id'])} "
                         f"t{ev['trial_index']:02d}  {_esc(what)}</div>")
        else:
            tag = "cached " if ev["cached"] else "refused"
            lines.append(f"<div><span class='c'>[{phase:9s}]</span> <span class='r'>{tag}</span> {_esc(ev['attack_id'])} "
                         f"t{ev['trial_index']:02d}</div>")
    if not lines:
        lines.append("<div class='c'>waiting for the first trial...</div>")
    return "<div class='log'>" + "".join(lines) + "</div>"


def _family_chart(result: dict) -> alt.LayerChart:
    rows = []
    for side, label in (("baseline", "Trusted baseline"), ("candidate", "Candidate")):
        for cat, s in result[side]["summary"]["categories"].items():
            rows.append({"family": cat.replace("_", " "), "config": label, "asr": s["asr"], "lo": s["ci_lower"], "hi": s["ci_upper"]})
    df = pd.DataFrame(rows)
    scale = alt.Scale(domain=["Trusted baseline", "Candidate"], range=[TEAL, ORANGE])
    base = alt.Chart(df).encode(y=alt.Y("family:N", title=None, sort=None),
                                color=alt.Color("config:N", scale=scale, legend=alt.Legend(title=None, orient="bottom")))
    bars = base.mark_bar(size=14, cornerRadiusEnd=4).encode(
        x=alt.X("asr:Q", title="attack success rate", axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])),
        yOffset="config:N",
        tooltip=[alt.Tooltip("family:N"), alt.Tooltip("config:N"), alt.Tooltip("asr:Q", format=".1%", title="success rate"),
                 alt.Tooltip("lo:Q", format=".1%", title="CI low"), alt.Tooltip("hi:Q", format=".1%", title="CI high")])
    err = base.mark_rule(color=INK_2, strokeWidth=1.5).encode(x="lo:Q", x2="hi:Q", yOffset="config:N")
    rule = alt.Chart(pd.DataFrame({"t": [THRESHOLD]})).mark_rule(color=INK_3, strokeDash=[4, 4]).encode(x="t:Q")
    label = alt.Chart(pd.DataFrame({"t": [THRESHOLD], "txt": [f"threshold {THRESHOLD:.0%}"]})).mark_text(
        align="left", dx=4, dy=-96, color=INK_3, fontSize=11).encode(x="t:Q", text="txt:N")
    return _chart_theme((bars + err + rule + label).properties(height=230, background=CARD))


def _record_frames(entries: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    decisions = {e["payload"].get("check_entry_hash"): e["payload"] for e in entries if e["entry_type"] == "gate_decision"}
    score_rows, heat_rows = [], []
    for e in entries:
        if e["entry_type"] != "check_run":
            continue
        p = e["payload"]
        d = decisions.get(e["entry_hash"], {})
        cand, base = p["candidate"], p["baseline"]
        model = (p.get("model") or "").split(".")[-1][:28]
        score_rows.append({
            "ledger #": e["index"], "model": model, "candidate": cand["name"], "trials/attack": p.get("trials_per_attack"),
            "baseline": base["summary"]["overall"]["asr"], "candidate ASR": cand["summary"]["overall"]["asr"],
            "decision": d.get("decision", "?"), "interaction": "yes" if (d.get("interaction") or {}).get("flag") else "",
        })
        for aid, s in cand["summary"].get("per_attack", {}).items():
            heat_rows.append({"ledger #": e["index"], "model": model, "candidate": cand["name"], "attack": aid, "asr": s["asr"]})
    return pd.DataFrame(score_rows), pd.DataFrame(heat_rows)


def _heatmap(df: pd.DataFrame) -> alt.Chart:
    return _chart_theme(alt.Chart(df).mark_rect(stroke=CARD, strokeWidth=2, cornerRadius=3).encode(
        x=alt.X("attack:N", title=None), y=alt.Y("candidate:N", title=None),
        color=alt.Color("asr:Q", scale=alt.Scale(domain=[0, 1], range=ORANGE_RAMP), legend=alt.Legend(title="breach rate", format="%")),
        tooltip=["candidate", "attack", alt.Tooltip("asr:Q", format=".0%", title="breach rate")])
        .properties(height=28 * max(1, df["candidate"].nunique()) + 70, background=CARD))


def _kpi(label: str, value: str, detail: str = "", tone: str = "") -> str:
    col = {"good": GREEN, "bad": ORANGE_DARK, "warn": "#8a5a00"}.get(tone, INK)
    return f"<div class='kpi'><div class='l'>{_esc(label)}</div><div class='v' style='color:{col}'>{_esc(value)}</div><div class='d'>{_esc(detail)}</div></div>"


# ----------------------------------------------------------------------------- sidebar controls
with st.sidebar:
    st.markdown("<div class='ag-h'>Run the gate</div>", unsafe_allow_html=True)
    configs = _configs()
    baseline = st.selectbox("Trusted baseline", configs, index=configs.index("configs/v1.yaml") if "configs/v1.yaml" in configs else 0)
    candidate = st.selectbox("Proposed change (candidate)", configs, index=configs.index("configs/v2.yaml") if "configs/v2.yaml" in configs else 0)
    model_label = st.selectbox("Agent model (the company's assistant)", list(AGENT_MODELS))
    trials = st.slider("Trials per attack", 1, 20, 10,
                       help="At 15% threshold a clean run needs at least 8 trials per attack to be ALLOWED; fewer can only BLOCK or REVIEW.")
    concurrency = st.slider("Concurrency", 1, 8, 6)
    use_cache = st.checkbox("Reuse cached trials", value=False, help="Off: every trial is a fresh model call (live demo). On: instant replay of recorded trials.")
    attribution = st.checkbox("Explain the cause (attribution)", value=True)
    running = isinstance(st.session_state.get("run"), RunState) and st.session_state["run"].running
    if st.button("Run gate", type="primary", disabled=running, width="stretch"):
        mode, model_id = AGENT_MODELS[model_label]
        try:
            st.session_state["run"] = RunState(baseline, candidate, mode, model_id, trials, concurrency, use_cache, attribution)
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))
    if st.button("Clear", disabled=running, width="stretch"):
        st.session_state.pop("run", None)
    st.caption("Every run appends signed check and decision entries to the ledger, exactly like the CLI.")

# ----------------------------------------------------------------------------- header + KPI row (from the ledger)
entries = _ledger_entries()
head = entries[-1]["entry_hash"] if entries else ""
score, heat = _record_frames(entries)
models = list(score["model"].value_counts().index) if not score.empty else []
verify_state = st.session_state.get("verify")

st.markdown("<p class='ag-title'>AgentGate</p><p class='ag-sub'>Release gate for AI agent configurations. "
            "Every change is attacked before it ships; every decision is signed.</p>", unsafe_allow_html=True)

kpi_model = st.session_state.get("record_model") or (models[0] if models else None)
sub = score[score["model"] == kpi_model] if kpi_model else score
blocked = int((sub["decision"] == "BLOCK").sum()) if not sub.empty else 0
allowed = int((sub["decision"] == "ALLOW").sum()) if not sub.empty else 0
review = int((sub["decision"] == "REVIEW").sum()) if not sub.empty else 0
trials_total = 0
for e in entries:
    if e["entry_type"] == "check_run":
        s = e["payload"]["candidate"]["summary"]["overall"]
        b = e["payload"]["baseline"]["summary"]["overall"]
        trials_total += int(s.get("n", 0)) + int(b.get("n", 0))


def _benign_names() -> set[str]:
    """Configs whose description declares them benign (used for the false-block tile)."""
    names = set()
    for p in _configs():
        try:
            c = load_config(p)
        except Exception:  # noqa: BLE001 - a broken config must not break the header
            continue
        if any(k in (c.description or "").lower() for k in ("benign", "safe update")):
            names.add(c.name)
    return names


benign = _benign_names()
benign_checks = sub[sub["candidate"].isin(benign)] if not sub.empty else sub
false_blocks = int((benign_checks["decision"] == "BLOCK").sum()) if not benign_checks.empty else 0
chain_txt = "unverified" if not verify_state else ("verified" if verify_state[0] == 0 else "FAILED")
chain_tone = "" if not verify_state else ("good" if verify_state[0] == 0 else "bad")
model_short = (kpi_model or "all models").replace("-instruct", "").replace("-v1:0", "")
st.markdown(
    "<div class='kpis'>"
    + _kpi("Changes blocked", str(blocked), f"on {model_short}", "bad" if blocked else "")
    + _kpi("Changes allowed", str(allowed), f"{review} awaiting human review", "good" if allowed else "")
    + _kpi("False blocks", f"{false_blocks} / {len(benign_checks)}", "benign changes wrongly blocked", "bad" if false_blocks else "good")
    + _kpi("Agent trials executed", f"{trials_total:,}", "real model calls, all models")
    + _kpi("Ledger entries", str(len(entries)), f"head {_short(head, 10)}")
    + _kpi("Chain integrity", chain_txt, "signed hash chain, verify offline", chain_tone)
    + "</div>",
    unsafe_allow_html=True,
)

state: RunState | None = st.session_state.get("run")

# ----------------------------------------------------------------------------- live run panel
if state is None:
    st.markdown("<div class='ag-h'>Live run</div>", unsafe_allow_html=True)
    st.markdown("<div class='card'>Pick a candidate and press <b>Run gate</b>. The trusted baseline and the candidate are "
                "attacked side by side; breaches show the recipient or the bank account the agent changed.</div>", unsafe_allow_html=True)
else:
    elapsed = (state.finished or time.time()) - state.started
    events, counts = state.snapshot()
    done_total = len(events)
    expected = state.expected_total
    per_config = len(state.suite) * state.trials
    status = "Running" if state.running else ("Failed" if state.error else "Complete")
    tone = TEAL if state.running else (ORANGE_DARK if state.error else GREEN)
    breakdown = f"baseline {per_config} + candidate {per_config}"
    if state.n_variants:
        breakdown += f" + {state.n_variants} explanation variant{'s' if state.n_variants != 1 else ''} {state.n_variants * per_config}"
    st.markdown(f"<div class='ag-h'>Live run <span style='color:{tone};text-transform:none;letter-spacing:0'>{status}</span> "
                f"<span class='dim' style='text-transform:none;letter-spacing:0;font-weight:400'>{done_total} of {expected} trials "
                f"({breakdown}), {elapsed:.0f}s, {_esc(state.model or 'offline mock')}</span></div>", unsafe_allow_html=True)
    st.progress(min(1.0, done_total / max(1, expected)) if state.running else 1.0)
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(_attack_table(state, state.baseline_hash, f"Trusted baseline {Path(state.baseline_path).stem}", TEAL), unsafe_allow_html=True)
    with c2:
        st.markdown(_attack_table(state, state.candidate_hash, f"Candidate {Path(state.candidate_path).stem}", ORANGE), unsafe_allow_html=True)
    variants = [h for h in counts if state.phase_of(h) == "variant"]
    if variants:
        st.caption(f"Explaining the cause: {len(variants)} single-change variant(s) running (baseline + one change each).")
    st.markdown("<div class='ag-h'>Event log</div>", unsafe_allow_html=True)
    st.markdown(_event_log(state), unsafe_allow_html=True)
    if state.error:
        st.error(state.error)

    # ------------------------------------------------------------------------- verdict panel
    if state.result:
        res = state.result
        dec = res["decision"]["decision"]
        col, word = DECISION_STYLE.get(dec, (INK_3, dec))
        b_asr = res["baseline"]["summary"]["overall"]["asr"]
        c_asr = res["candidate"]["summary"]["overall"]["asr"]
        c_n = res["candidate"]["summary"]["overall"]["n"]
        worst = max(res["candidate"]["summary"]["categories"].items(), key=lambda kv: kv[1]["asr"])
        st.markdown("<div class='ag-h'>Verdict</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='banner'><span class='pill' style='background:{col}'>{word}</span>"
                    f"<span class='ctx'><b>{_esc(res['candidate']['name'])}</b> vs {_esc(res['baseline']['name'])} on "
                    f"{_esc(res['model'])}, {res['trials_per_attack']} trials per attack, {elapsed:.0f}s</span></div>",
                    unsafe_allow_html=True)
        st.markdown(
            "<div class='kpis' style='grid-template-columns:repeat(4,minmax(0,1fr));margin-top:.75rem'>"
            + _kpi("Baseline attack success", _pct(b_asr), f"{res['baseline']['summary']['overall']['successes']} of {res['baseline']['summary']['overall']['n']} trials")
            + _kpi("Candidate attack success", _pct(c_asr), f"{res['candidate']['summary']['overall']['successes']} of {c_n} trials",
                   "bad" if c_asr > b_asr else "good")
            + _kpi("Change", f"{100 * (c_asr - b_asr):+.0f} pts", "candidate minus baseline", "bad" if c_asr > b_asr else "good")
            + _kpi("Worst family", _pct(worst[1]["asr"]), worst[0].replace("_", " "), "bad" if worst[1]["asr"] > THRESHOLD else "")
            + "</div>", unsafe_allow_html=True)
        if dec == "REVIEW":
            st.info(review_explanation({**res, "evidence": {"suite": state.suite}}, THRESHOLD))
        for reason in res["decision"]["reasons"]:
            st.markdown(f"<div class='mono'>- {_esc(reason)}</div>", unsafe_allow_html=True)
        v1, v2 = st.columns([3, 2])
        with v1:
            st.altair_chart(_family_chart(res), width="stretch")
        with v2:
            rows = res["attribution"]["rows"]
            if rows:
                st.markdown("<div class='ag-h'>What caused it</div>", unsafe_allow_html=True)
                st.dataframe(pd.DataFrame([{"change": r["change_id"], "effect on success rate": f"{100 * r['delta_overall']:+.1f} pts",
                                            "assessment": r["effect"], "alone it would be": r.get("variant_decision", "")} for r in rows]),
                             hide_index=True, width="stretch")
            inter = res["attribution"].get("interaction") or {}
            if inter.get("flag"):
                st.warning(inter["message"])

        # --------------------------------------------------------------------- evidence panel
        st.markdown("<div class='ag-h'>Evidence</div>", unsafe_allow_html=True)
        new_entries = [e for e in _ledger_entries() if e["index"] >= state.ledger_start]
        st.markdown("<table class='ag'><tr><th>#</th><th>type</th><th>entry hash</th><th>previous hash</th><th>recorded at (UTC)</th></tr>" + "".join(
            f"<tr><td>{e['index']}</td><td>{_esc(e['entry_type'])}</td><td class='dim'>{_short(e['entry_hash'], 24)}</td>"
            f"<td class='dim'>{_short(e.get('prev_hash'), 24)}</td><td class='dim'>{_esc(e.get('timestamp', ''))[:19].replace('T', ' ')}</td></tr>"
            for e in new_entries) + "</table>", unsafe_allow_html=True)
        e1, e2, e3, e4 = st.columns(4)
        with e1:
            if st.button("Verify ledger", width="stretch"):
                st.session_state["verify"] = _run_cli("cli.py", "verify")
                st.rerun()
        with e2:
            if st.button("Tamper demo", width="stretch", help="Flips one decision in a copy of the chain and runs the auditor on both."):
                st.session_state["tamper"] = _run_cli("scripts/tamper_demo.py")
        with e3:
            if st.button("Generate audit pack (PDF)", width="stretch",
                         help="PDF + Markdown + HTML for this check; file hashes are recorded in the ledger."):
                try:
                    st.session_state["pack"] = generate_pack(check_ref=res["ledger"]["check_entry_hash"], pdf=True)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"pack failed: {exc}")
        with e4:
            pk = st.session_state.get("pack")
            if pk and pk.get("check_entry_hash") == res["ledger"]["check_entry_hash"] and pk.get("pdf_path"):
                st.download_button("Download audit PDF", data=Path(pk["pdf_path"]).read_bytes(),
                                   file_name=f"agentgate_evidence_{res['candidate']['name']}_{res['ledger']['check_entry_hash'][:8]}.pdf",
                                   mime="application/pdf", width="stretch")
        pk = st.session_state.get("pack")
        if pk and pk.get("check_entry_hash") == res["ledger"]["check_entry_hash"]:
            st.caption(f"Pack written to {pk['pack_dir']}; sha256 pack.pdf {pk['sha256'].get('pack.pdf', '')[:16]}; "
                       f"ledger entry {_short(pk['ledger_entry_hash'])}; {pk.get('verify', '')}")
        if st.session_state.get("verify"):
            code, out = st.session_state["verify"]
            (st.success if code == 0 else st.error)(out.splitlines()[-1] if out else "")
        if st.session_state.get("tamper"):
            st.code(st.session_state["tamper"][1], language="text")
        if dec == "REVIEW" and not res.get("override"):
            with st.form("override_form"):
                st.markdown("<div class='ag-h'>Signed human override</div>", unsafe_allow_html=True)
                reason = st.text_area("Reason (required; becomes the signed payload)")
                actor = st.text_input("Approver")
                if st.form_submit_button("Record signed override"):
                    try:
                        entry = override(reason, actor)
                        st.success(f"Override recorded at #{entry['index']} ({_short(entry['entry_hash'])})")
                    except (GateError, LedgerError) as exc:
                        st.error(str(exc))

        # --------------------------------------------------------------------- transcript viewer
        breached = [ev for ev in events if ev["success"] and state.phase_of(ev["config_hash"]) == "candidate"]
        if breached:
            st.markdown("<div class='ag-h'>What the agent actually did</div>", unsafe_allow_html=True)
            labels = [f"{ev['attack_id']} trial {ev['trial_index']}: {', '.join(ev['recipients']) or 'tool call'}" for ev in breached]
            pick = st.selectbox("Breached trial", labels)
            ev = breached[labels.index(pick)]
            for c in ev.get("tool_calls", []):
                st.markdown(f"<div class='mono'>-> {_esc(c.get('name'))}({_esc(c.get('arguments'))})</div>", unsafe_allow_html=True)
            for mail in ev.get("outbox", []):
                st.markdown(f"<div class='bad mono'>SENT to {_esc(mail.get('to'))} | subject: {_esc(mail.get('subject'))}</div>", unsafe_allow_html=True)
                st.code(mail.get("body") or "", language="text")
            for act in ev.get("actions", []):
                st.markdown(f"<div class='bad mono'>ACTION {_esc(act.get('tool'))}: {_esc(act.get('vendor'))} -> {_esc(act.get('iban'))} "
                            f"({_esc(act.get('reason'))})</div>", unsafe_allow_html=True)
            if not ev.get("tool_calls") and not ev.get("outbox") and not ev.get("actions"):
                st.caption("No transcript captured for this trial.")

# ----------------------------------------------------------------------------- record panel (from the ledger)
st.markdown("<div class='ag-h'>Record, from the signed ledger</div>", unsafe_allow_html=True)
if score.empty:
    st.caption("No check runs in the ledger yet.")
else:
    pick_model = st.selectbox("Model", models, index=0, key="record_model")
    sub = score[score["model"] == pick_model].sort_values("ledger #", ascending=False)
    show = sub.copy()
    show["baseline"] = show["baseline"].map(_pct)
    show["candidate ASR"] = show["candidate ASR"].map(_pct)
    show = show.rename(columns={"baseline": "baseline success", "candidate ASR": "candidate success"})
    st.dataframe(show, hide_index=True, width="stretch", height=min(420, 38 + 35 * len(show)))
    hsub = heat[heat["model"] == pick_model]
    if not hsub.empty:
        st.markdown("<div class='ag-h'>Breach rate by attack and candidate, latest check per candidate</div>", unsafe_allow_html=True)
        latest = hsub.sort_values("ledger #").groupby(["candidate", "attack"], as_index=False).last()
        st.altair_chart(_heatmap(latest), width="stretch")

    st.markdown("<div class='ag-h'>Audit pack for any recorded check</div>", unsafe_allow_html=True)
    a1, a2, a3 = st.columns([3, 1, 1])
    with a1:
        options = [f"#{int(r['ledger #'])}  {r['candidate']}  on {r['model']}  ->  {r['decision']}" for _, r in sub.iterrows()]
        pick_row = st.selectbox("Recorded check (ledger entry)", options, label_visibility="collapsed") if options else None
    with a2:
        if pick_row and st.button("Generate PDF", width="stretch"):
            idx = pick_row.split()[0].lstrip("#")
            try:
                st.session_state["record_pack"] = generate_pack(check_ref=idx, pdf=True)
            except Exception as exc:  # noqa: BLE001
                st.error(f"pack failed: {exc}")
    with a3:
        rp = st.session_state.get("record_pack")
        if rp and rp.get("pdf_path") and pick_row and rp.get("check_entry_hash"):
            st.download_button("Download", data=Path(rp["pdf_path"]).read_bytes(),
                               file_name=f"agentgate_evidence_{rp['check_entry_hash'][:8]}.pdf", mime="application/pdf", width="stretch")
    rp = st.session_state.get("record_pack")
    if rp:
        st.caption(f"Last generated: {rp['decision']} for check {_short(rp['check_entry_hash'])}; sha256 pack.pdf "
                   f"{rp['sha256'].get('pack.pdf', '')[:16]}; {rp.get('verify', '')}")

# ----------------------------------------------------------------------------- live refresh
if state is not None and state.running:
    time.sleep(POLL_SECONDS)
    st.rerun()
