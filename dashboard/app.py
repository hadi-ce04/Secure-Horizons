"""AgentGate live dashboard.

Run from the repository root:  streamlit run dashboard/app.py

One screen, five panels:

1. Status bar: ledger length, head hash, last verify result, model in use.
2. Live run: pick baseline / candidate / agent model / trials, press RUN. A
   background thread executes the real gate (``gate.gate.run_check``) and
   every finished trial streams into the per-attack tables and the event log.
   Every run writes to the real ledger, exactly like the CLI.
3. Verdict: decision banner, per-family intervals vs the threshold,
   attribution and the interaction callout.
4. Evidence: the ledger entries this run appended, verify, tamper demo,
   signed override (REVIEW only), evidence pack download.
5. Record: scorecard and attack x config heatmap built from the whole ledger,
   plus a transcript viewer for any breached trial of the current run.
"""

from __future__ import annotations

import glob
import html
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from evidence.pack import generate_pack  # noqa: E402
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
C_BG, C_PANEL, C_TEXT, C_DIM = "#0b0f14", "#111827", "#d1d5db", "#6b7280"
C_CYAN, C_GREEN, C_RED, C_AMBER = "#22d3ee", "#22c55e", "#ef4444", "#f59e0b"
DECISION_COLORS = {"ALLOW": C_GREEN, "BLOCK": C_RED, "REVIEW": C_AMBER}
POLL_SECONDS = 0.5

st.set_page_config(page_title="AgentGate", page_icon=":shield:", layout="wide")

CSS = f"""
<style>
  html, body, [class*="css"] {{ font-family: "JetBrains Mono", Consolas, "Courier New", monospace; }}
  .ag-status {{ display:flex; gap:1.5rem; flex-wrap:wrap; padding:.6rem .9rem; border:1px solid #1f2937;
               border-radius:6px; background:{C_PANEL}; font-size:.85rem; }}
  .ag-status b {{ color:{C_CYAN}; font-weight:500; }}
  .ag-h {{ color:{C_CYAN}; letter-spacing:.12em; text-transform:uppercase; font-size:.8rem; margin:1.2rem 0 .4rem; }}
  .ag-banner {{ padding:1rem 1.2rem; border-radius:8px; border:1px solid; font-size:1.6rem; font-weight:500; letter-spacing:.1em; }}
  table.ag {{ width:100%; border-collapse:collapse; font-size:.8rem; }}
  table.ag th {{ text-align:left; color:{C_DIM}; font-weight:500; padding:.25rem .4rem; border-bottom:1px solid #1f2937; }}
  table.ag td {{ padding:.25rem .4rem; border-bottom:1px solid #111827; white-space:nowrap; }}
  .bar {{ display:inline-block; height:.6rem; background:{C_RED}; vertical-align:middle; border-radius:2px; }}
  .barbg {{ display:inline-block; width:90px; height:.6rem; background:#1f2937; vertical-align:middle; border-radius:2px; }}
  .ok {{ color:{C_GREEN}; }} .bad {{ color:{C_RED}; }} .dim {{ color:{C_DIM}; }} .warn {{ color:{C_AMBER}; }}
  .log {{ background:#05070a; border:1px solid #1f2937; border-radius:6px; padding:.6rem .8rem; height:260px;
          overflow:auto; white-space:nowrap; font-size:.78rem; line-height:1.45; }}
  .log .b {{ color:{C_RED}; }} .log .r {{ color:{C_GREEN}; }} .log .c {{ color:{C_DIM}; }}
  .mono {{ font-family: inherit; font-size:.78rem; color:{C_DIM}; }}
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


class RunState:
    """Shared between the UI thread and the worker thread."""

    def __init__(self, baseline: str, candidate: str, mode: str, model: str | None, trials: int,
                 concurrency: int, use_cache: bool, attribution: bool):
        self.baseline_path, self.candidate_path = baseline, candidate
        self.mode, self.model, self.trials = mode, model, trials
        self.concurrency, self.use_cache, self.attribution = concurrency, use_cache, attribution
        self.baseline_hash = load_config(baseline).config_hash
        self.candidate_hash = load_config(candidate).config_hash
        self.suite = load_suite()
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.counts: dict[str, dict[str, list[int]]] = {}  # hash -> attack -> [done, breached]
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
                self.candidate_path,
                baseline_path=self.baseline_path,
                mode=self.mode,
                trials_per_attack=self.trials,
                concurrency=self.concurrency,
                use_cache=self.use_cache,
                ollama_model=self.model,
                run_attribution=self.attribution,
                progress=self._progress,
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


def _attack_table(state: RunState, config_hash: str | None, title: str) -> str:
    _, counts = state.snapshot()
    per = counts.get(config_hash or "", {})
    rows = []
    tot_done = tot_bad = 0
    for a in state.suite:
        done, bad = per.get(a["id"], [0, 0])
        tot_done += done
        tot_bad += bad
        rate = (bad / done) if done else 0.0
        width = int(90 * rate)
        cls = "bad" if bad else ("ok" if done else "dim")
        chan = {"user": "usr", "email": "mail", "document": "doc", "tool_description": "tool"}.get(a["channel"], a["channel"])
        rows.append(
            f"<tr><td title='{_esc(a['category'])} via {_esc(a['channel'])}'>{_esc(a['id'])} <span class='dim'>{chan}</span></td>"
            f"<td class='dim'>{_esc(a['owasp'].split(' ')[0])}</td>"
            f"<td>{done}/{state.trials}</td><td class='ok'>{done - bad}</td><td class='{cls}'>{bad}</td>"
            f"<td><span class='barbg'><span class='bar' style='width:{width}px'></span></span> {rate:.0%}</td></tr>"
        )
    total = len(state.suite) * state.trials
    head = (f"<div class='ag-h'>{_esc(title)} <span class='dim'>{tot_done}/{total} trials, "
            f"<span class='{'bad' if tot_bad else 'ok'}'>{tot_bad} breached</span></span></div>")
    return head + ("<div style='overflow-x:auto'><table class='ag'><tr><th>attack</th><th>owasp</th>"
                   "<th>trials</th><th>refused</th><th>breached</th><th>rate</th></tr>" + "".join(rows) + "</table></div>")


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


def _ci_chart(result: dict) -> alt.LayerChart:
    rows = []
    for side in ("baseline", "candidate"):
        name = result[side]["name"]
        for cat, s in result[side]["summary"]["categories"].items():
            rows.append({"family": cat, "config": f"{side}: {name}", "asr": s["asr"], "lo": s["ci_lower"], "hi": s["ci_upper"]})
    df = pd.DataFrame(rows)
    base = alt.Chart(df).encode(y=alt.Y("family:N", title=None), color=alt.Color("config:N", legend=alt.Legend(title=None, orient="bottom")))
    bars = base.mark_bar(opacity=0.85).encode(x=alt.X("asr:Q", title="attack success rate", scale=alt.Scale(domain=[0, 1])),
                                               yOffset="config:N")
    err = base.mark_rule(color=C_TEXT).encode(x="lo:Q", x2="hi:Q", yOffset="config:N")
    rule = alt.Chart(pd.DataFrame({"t": [THRESHOLD]})).mark_rule(color=C_AMBER, strokeDash=[4, 4]).encode(x="t:Q")
    return (bars + err + rule).properties(height=260, background=C_BG).configure_axis(labelColor=C_TEXT, titleColor=C_DIM,
                                                                                         gridColor="#1f2937").configure_legend(labelColor=C_TEXT)


def _record_frames(entries: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Scorecard and heatmap frames from the ledger (check_run + gate_decision pairs)."""
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
            "baseline ASR": round(base["summary"]["overall"]["asr"], 3), "candidate ASR": round(cand["summary"]["overall"]["asr"], 3),
            "decision": d.get("decision", "?"),
            "interaction": "yes" if (d.get("interaction") or {}).get("flag") else "",
        })
        for aid, s in cand["summary"].get("per_attack", {}).items():
            heat_rows.append({"ledger #": e["index"], "model": model, "candidate": cand["name"], "attack": aid, "asr": s["asr"]})
    return pd.DataFrame(score_rows), pd.DataFrame(heat_rows)


def _heatmap(df: pd.DataFrame) -> alt.Chart:
    return (alt.Chart(df).mark_rect(stroke=C_BG).encode(
        x=alt.X("attack:N", title=None), y=alt.Y("candidate:N", title=None),
        color=alt.Color("asr:Q", scale=alt.Scale(domain=[0, 1], range=["#0f2a1f", C_GREEN, C_AMBER, C_RED]), title="ASR"),
        tooltip=["candidate", "attack", alt.Tooltip("asr:Q", format=".2f")])
        .properties(height=26 * max(1, df["candidate"].nunique()) + 60, background=C_BG)
        .configure_axis(labelColor=C_TEXT, titleColor=C_DIM).configure_legend(labelColor=C_TEXT, titleColor=C_DIM))


def _load_trial_record(state: RunState, ev: dict) -> dict | None:
    model = state.result["model"] if state.result else (state.model or "offline-mock")
    path = Path(DEFAULT_CACHE_DIR) / f"{cache_key(ev['config_hash'], ev['attack_id'], ev['trial_index'], state.mode, model)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ----------------------------------------------------------------------------- sidebar controls
with st.sidebar:
    st.markdown("<div class='ag-h'>Run the gate</div>", unsafe_allow_html=True)
    configs = _configs()
    baseline = st.selectbox("Baseline (trusted) config", configs, index=configs.index("configs/v1.yaml") if "configs/v1.yaml" in configs else 0)
    candidate = st.selectbox("Candidate config", configs, index=configs.index("configs/v2.yaml") if "configs/v2.yaml" in configs else 0)
    model_label = st.selectbox("Agent model (the company's assistant)", list(AGENT_MODELS))
    trials = st.slider("Trials per attack", 1, 20, 5)
    concurrency = st.slider("Concurrency", 1, 8, 6)
    use_cache = st.checkbox("Reuse cached trials", value=False, help="Off = every trial is a fresh model call (live demo). On = instant replay of recorded trials.")
    attribution = st.checkbox("Run attribution variants", value=True)
    running = isinstance(st.session_state.get("run"), RunState) and st.session_state["run"].running
    if st.button("RUN GATE", type="primary", disabled=running, use_container_width=True):
        mode, model_id = AGENT_MODELS[model_label]
        try:
            st.session_state["run"] = RunState(baseline, candidate, mode, model_id, trials, concurrency, use_cache, attribution)
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))
    if st.button("Clear screen", disabled=running, use_container_width=True):
        st.session_state.pop("run", None)
    st.markdown("<div class='mono'>Every run appends check_run and gate_decision entries to the real ledger, like the CLI.</div>",
                unsafe_allow_html=True)

# ----------------------------------------------------------------------------- status bar
entries = _ledger_entries()
head = entries[-1]["entry_hash"] if entries else ""
verify_state = st.session_state.get("verify")
verify_html = ("<span class='dim'>not checked</span>" if not verify_state else
               (f"<span class='ok'>verified</span>" if verify_state[0] == 0 else "<span class='bad'>FAILED</span>"))
st.markdown(
    f"<div class='ag-status'><span><b>AGENTGATE</b> release gate for LLM agent configs</span>"
    f"<span>ledger <b>{len(entries)}</b> entries</span><span>head <b>{_short(head)}</b></span>"
    f"<span>chain {verify_html}</span><span>threshold <b>{THRESHOLD:.2f}</b> per family</span>"
    f"<span>agent <b>{_esc(model_label)}</b></span></div>",
    unsafe_allow_html=True,
)

state: RunState | None = st.session_state.get("run")

# ----------------------------------------------------------------------------- live run panel
if state is None:
    st.markdown("<div class='ag-h'>Live run</div>", unsafe_allow_html=True)
    st.markdown("<div class='mono'>Pick a candidate and press RUN GATE. Baseline and candidate tables fill as trials finish; "
                "breaches show the recipient or the bank account the agent changed.</div>", unsafe_allow_html=True)
else:
    elapsed = (state.finished or time.time()) - state.started
    events, counts = state.snapshot()
    total_expected = len(state.suite) * state.trials * (2 if not state.attribution else 2)  # variants are unknown up front
    done_total = len(events)
    status = "RUNNING" if state.running else ("FAILED" if state.error else "COMPLETE")
    colour = C_CYAN if state.running else (C_RED if state.error else C_GREEN)
    st.markdown(f"<div class='ag-h'>Live run <span style='color:{colour}'>{status}</span> "
                f"<span class='dim'>{done_total} trials streamed, {elapsed:.0f}s, mode {state.mode}"
                f"{', model ' + state.model if state.model else ''}</span></div>", unsafe_allow_html=True)
    st.progress(min(1.0, done_total / max(1, total_expected)) if state.running else 1.0)
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(_attack_table(state, state.baseline_hash, f"Baseline {Path(state.baseline_path).stem}"), unsafe_allow_html=True)
    with c2:
        st.markdown(_attack_table(state, state.candidate_hash, f"Candidate {Path(state.candidate_path).stem}"), unsafe_allow_html=True)
    variant_hashes = [h for h in counts if state.phase_of(h) == "variant"]
    if variant_hashes:
        st.markdown(f"<div class='mono'>attribution variants running: {len(variant_hashes)} (baseline + one change each)</div>",
                    unsafe_allow_html=True)
    st.markdown("<div class='ag-h'>Event log</div>", unsafe_allow_html=True)
    st.markdown(_event_log(state), unsafe_allow_html=True)
    if state.error:
        st.error(state.error)

    # ------------------------------------------------------------------------- verdict panel
    if state.result:
        res = state.result
        dec = res["decision"]["decision"]
        col = DECISION_COLORS.get(dec, C_DIM)
        st.markdown(f"<div class='ag-h'>Verdict</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='ag-banner' style='border-color:{col};color:{col};background:{C_PANEL}'>{dec}"
                    f"<span style='font-size:.9rem;color:{C_DIM};margin-left:1rem'>{_esc(res['candidate']['name'])} vs "
                    f"{_esc(res['baseline']['name'])} on {_esc(res['model'])}, {res['trials_per_attack']} trials/attack</span></div>",
                    unsafe_allow_html=True)
        for reason in res["decision"]["reasons"]:
            st.markdown(f"<div class='mono'>- {_esc(reason)}</div>", unsafe_allow_html=True)
        v1, v2 = st.columns([3, 2])
        with v1:
            st.altair_chart(_ci_chart(res), use_container_width=True)
        with v2:
            rows = res["attribution"]["rows"]
            if rows:
                st.markdown("<div class='ag-h'>Attribution</div>", unsafe_allow_html=True)
                st.dataframe(pd.DataFrame([{"change": r["change_id"], "delta": round(r["delta_overall"], 3), "effect": r["effect"],
                                            "variant": r.get("variant_decision", "")} for r in rows]), hide_index=True, use_container_width=True)
            inter = res["attribution"].get("interaction") or {}
            if inter.get("flag"):
                st.markdown(f"<div class='warn'>{_esc(inter['message'])}</div>", unsafe_allow_html=True)

        # --------------------------------------------------------------------- evidence panel
        st.markdown("<div class='ag-h'>Evidence</div>", unsafe_allow_html=True)
        new_entries = [e for e in _ledger_entries() if e["index"] >= state.ledger_start]
        st.markdown("<table class='ag'><tr><th>#</th><th>type</th><th>entry hash</th><th>prev hash</th><th>at</th></tr>" + "".join(
            f"<tr><td>{e['index']}</td><td>{_esc(e['entry_type'])}</td><td class='dim'>{_short(e['entry_hash'], 24)}</td>"
            f"<td class='dim'>{_short(e.get('prev_hash'), 24)}</td><td class='dim'>{_esc(e.get('timestamp', ''))[:19]}</td></tr>"
            for e in new_entries) + "</table>", unsafe_allow_html=True)
        e1, e2, e3, e4 = st.columns(4)
        with e1:
            if st.button("Verify ledger", use_container_width=True):
                st.session_state["verify"] = _run_cli("cli.py", "verify")
                st.rerun()
        with e2:
            if st.button("Tamper demo", use_container_width=True, help="Flips one decision in a COPY of the chain and runs the auditor on both."):
                st.session_state["tamper"] = _run_cli("scripts/tamper_demo.py")
        with e3:
            if st.button("Evidence pack", use_container_width=True):
                try:
                    pk = generate_pack(candidate_path=state.candidate_path, baseline_path=state.baseline_path,
                                       mode=state.mode, trials_per_attack=state.trials)
                    st.session_state["pack"] = pk
                except Exception as exc:  # noqa: BLE001
                    st.error(f"pack failed: {exc}")
        with e4:
            pk = st.session_state.get("pack")
            if pk:
                st.download_button("Download pack.md", data=Path(pk["md_path"]).read_text(encoding="utf-8"),
                                   file_name="agentgate_evidence_pack.md", mime="text/markdown", use_container_width=True)
        if st.session_state.get("verify"):
            code, out = st.session_state["verify"]
            st.markdown(f"<div class='{'ok' if code == 0 else 'bad'} mono'>{_esc(out.splitlines()[-1] if out else '')}</div>", unsafe_allow_html=True)
        if st.session_state.get("tamper"):
            code, out = st.session_state["tamper"]
            st.code(out, language="text")
        if dec == "REVIEW" and not res.get("override"):
            with st.form("override_form"):
                st.markdown("<div class='ag-h'>Signed human override</div>", unsafe_allow_html=True)
                reason = st.text_area("Reason (required, becomes the signed payload)")
                actor = st.text_input("Actor")
                if st.form_submit_button("Record signed override"):
                    try:
                        entry = override(reason, actor)
                        st.success(f"override recorded at #{entry['index']} ({_short(entry['entry_hash'])})")
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
                rec = _load_trial_record(state, ev)
                st.markdown("<div class='dim mono'>no transcript captured for this trial"
                            f"{' (cached record found)' if rec else ''}</div>", unsafe_allow_html=True)

# ----------------------------------------------------------------------------- record panel (from the ledger)
st.markdown("<div class='ag-h'>Record (from the signed ledger)</div>", unsafe_allow_html=True)
score, heat = _record_frames(entries)
if score.empty:
    st.markdown("<div class='dim mono'>no check runs in the ledger yet</div>", unsafe_allow_html=True)
else:
    models = list(score["model"].value_counts().index)  # most-tested model first
    pick_model = st.selectbox("Model", models, index=0, key="record_model")
    sub = score[score["model"] == pick_model]
    st.dataframe(sub.sort_values("ledger #", ascending=False), hide_index=True, use_container_width=True, height=min(400, 38 + 35 * len(sub)))
    caught = int((sub["decision"] == "BLOCK").sum())
    allowed = int((sub["decision"] == "ALLOW").sum())
    st.markdown(f"<div class='mono'>{len(sub)} checks on this model: <span class='bad'>{caught} BLOCK</span>, "
                f"<span class='ok'>{allowed} ALLOW</span>, {len(sub) - caught - allowed} REVIEW</div>", unsafe_allow_html=True)
    hsub = heat[heat["model"] == pick_model]
    if not hsub.empty:
        st.markdown("<div class='ag-h'>Attack x candidate, latest check per candidate</div>", unsafe_allow_html=True)
        latest = hsub.sort_values("ledger #").groupby(["candidate", "attack"], as_index=False).last()
        st.altair_chart(_heatmap(latest), use_container_width=True)

# ----------------------------------------------------------------------------- live refresh
if state is not None and state.running:
    time.sleep(POLL_SECONDS)
    st.rerun()
