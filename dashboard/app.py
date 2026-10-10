"""AgentGate single-screen Streamlit dashboard.

Run with ``streamlit run dashboard/app.py`` from the repository root.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from evidence.pack import BADGE_COLORS, generate_pack, list_packs  # noqa: E402
from gate.config import list_configs  # noqa: E402
from gate.gate import THRESHOLD, GateError, load_latest_check, override, run_check  # noqa: E402
from ledger.ledger import LedgerError, get_default_ledger  # noqa: E402

st.set_page_config(page_title="AgentGate", layout="wide")


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _badge_html(decision: str, threshold: float) -> str:
    colors = BADGE_COLORS.get(decision, BADGE_COLORS["REVIEW"])
    return (
        f'<div role="status" style="display:inline-block;padding:0.5rem 1.2rem;border:2px solid {colors["fg"]};'
        f'border-radius:0.4rem;background:{colors["bg"]};color:{colors["fg"]};font-size:1.4rem;font-weight:700;">'
        f"Decision: {decision}</div>"
        f'<span style="margin-left:1rem;">Threshold T = {threshold:.2f}</span>'
    )


def _asr_frame(check: dict) -> pd.DataFrame:
    rows = []
    for side in ("baseline", "candidate"):
        for cat, s in check[side]["summary"]["categories"].items():
            rows.append(
                {
                    "category": cat,
                    "config": side,
                    "asr": s["asr"],
                    "ci_lower": s["ci_lower"],
                    "ci_upper": s["ci_upper"],
                    "n": s["n"],
                    "successes": s["successes"],
                }
            )
    return pd.DataFrame(rows)


def _asr_chart(df: pd.DataFrame) -> alt.LayerChart:
    base = alt.Chart(df).encode(
        x=alt.X("category:N", title="Attack category"),
        xOffset=alt.XOffset("config:N"),
        color=alt.Color("config:N", title="Config"),
    )
    points = base.mark_point(filled=True, size=80).encode(
        y=alt.Y("asr:Q", title="Attack success rate", scale=alt.Scale(domain=[0, 1])),
        tooltip=["category", "config", "asr", "ci_lower", "ci_upper", "n", "successes"],
    )
    bars = base.mark_rule(strokeWidth=2).encode(y="ci_lower:Q", y2="ci_upper:Q")
    threshold_df = pd.DataFrame({"y": [THRESHOLD], "label": [f"T = {THRESHOLD:.2f}"]})
    rule = alt.Chart(threshold_df).mark_rule(strokeDash=[6, 4], color="#b71c1c").encode(y="y:Q")
    label = alt.Chart(threshold_df).mark_text(align="left", dx=4, dy=-6, color="#b71c1c").encode(
        y="y:Q", text="label:N", x=alt.value(0)
    )
    return alt.layer(bars, points, rule, label).properties(height=360)


def _attribution_frame(rows: list[dict]) -> pd.DataFrame:
    records = []
    for r in rows:
        rec = {
            "change": r["change_id"],
            "kind": r["kind"],
            "delta overall": round(r["delta_overall"], 3),
        }
        for cat, d in sorted(r.get("delta_by_category", {}).items()):
            rec[f"delta {cat}"] = round(d, 3)
        rec["effect"] = r["effect"]
        rec["variant decision"] = r.get("variant_decision", "")
        records.append(rec)
    return pd.DataFrame(records)


# ----------------------------------------------------------------- sidebar
config_options = [_rel(p) for p in list_configs()]
default_idx = config_options.index("configs/v1.yaml") if "configs/v1.yaml" in config_options else 0

with st.sidebar:
    st.header("Run a check")
    baseline_choice = st.selectbox("Baseline config", config_options, index=default_idx, key="baseline_cfg")
    candidate_choice = st.selectbox("Candidate config", config_options, index=default_idx, key="candidate_cfg")
    mode_choice = st.radio("Mode", ["offline", "ollama"], index=0, key="mode")
    trials_choice = st.number_input("Trials per attack", min_value=1, value=10, step=1, key="trials")
    concurrency_choice = st.number_input("Concurrency", min_value=1, value=8, step=1, key="concurrency")
    use_cache_choice = st.checkbox("Use cache", value=True, key="use_cache")
    ollama_model_choice = st.text_input("Ollama model (optional)", value="", key="ollama_model")
    if st.button("Run check", key="run_check"):
        with st.spinner("Running attack suite"):
            try:
                st.session_state["check"] = run_check(
                    candidate_choice,
                    baseline_path=baseline_choice,
                    mode=mode_choice,
                    trials_per_attack=int(trials_choice),
                    concurrency=int(concurrency_choice),
                    use_cache=use_cache_choice,
                    ollama_model=ollama_model_choice.strip() or None,
                )
            except Exception as exc:  # surfaced to the user in the UI
                st.error(f"Check failed: {exc}")

# -------------------------------------------------------------- main view
st.title("AgentGate")
st.caption("Statistical security release gate for LLM agent configurations")

check = st.session_state.get("check") or load_latest_check()

st.subheader("Gate status")
if check is None:
    st.info("No check has been run yet. Use 'Run check' in the sidebar.")
else:
    decision = check["decision"]
    st.markdown(_badge_html(decision["decision"], decision["threshold"]), unsafe_allow_html=True)
    st.write(
        f"Baseline **{check['baseline']['name']}** (`{check['baseline']['hash'][:12]}`) vs candidate "
        f"**{check['candidate']['name']}** (`{check['candidate']['hash'][:12]}`), mode {check['mode']}, "
        f"{check['trials_per_attack']} trials per attack, generated {check['generated_at']}."
    )
    st.markdown("\n".join(f"- {r}" for r in decision["reasons"]))
    if check.get("override"):
        ov = check["override"]
        st.success(f"Overridden by {ov['actor']}: {ov['reason']} (entry {ov['entry_hash'][:16]})")

    st.subheader("Attack success rate with Wilson 95% CI")
    df = _asr_frame(check)
    st.altair_chart(_asr_chart(df))
    st.dataframe(df[["category", "config", "n", "successes", "asr", "ci_lower", "ci_upper"]], hide_index=True)

    st.subheader("Attribution breakdown")
    rows = check.get("attribution", {}).get("rows", [])
    if rows:
        st.dataframe(_attribution_frame(rows), hide_index=True)
    else:
        st.write("No changes between baseline and candidate.")

    if decision["decision"] == "REVIEW" and not check.get("override"):
        st.subheader("Manual override")
        with st.form("override_form"):
            reason = st.text_area("Reason (required)", key="override_reason")
            actor = st.text_input("Actor", value="", key="override_actor")
            submitted = st.form_submit_button("Record signed override")
        if submitted:
            try:
                entry = override(reason, actor)
                st.success(f"Override recorded at index {entry['index']} ({entry['entry_hash'][:16]})")
                st.session_state["check"] = load_latest_check()
            except (GateError, LedgerError) as exc:
                st.error(str(exc))

st.subheader("Ledger audit")
try:
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "ledger" / "verify.py")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        st.success(f"Verified: {output}")
    else:
        st.error(f"Verification FAILED: {output}")
except subprocess.TimeoutExpired:
    st.error("Ledger verification timed out.")

try:
    entries = get_default_ledger().read_all()
except LedgerError as exc:
    entries = []
    st.error(str(exc))
if entries:
    recent = [
        {
            "index": e["index"],
            "timestamp": e["timestamp"],
            "entry_type": e["entry_type"],
            "entry_hash": e["entry_hash"][:16],
        }
        for e in entries[-10:][::-1]
    ]
    st.dataframe(pd.DataFrame(recent), hide_index=True)
else:
    st.write("Ledger is empty.")

st.subheader("Evidence packs")
if st.button("Generate evidence pack", key="generate_pack"):
    with st.spinner("Generating evidence pack"):
        try:
            res = generate_pack()
            st.success(f"Pack generated: {_rel(Path(res['pack_dir']))}")
        except Exception as exc:  # surfaced to the user in the UI
            st.error(f"Pack generation failed: {exc}")

packs = list_packs()
if packs:
    newest = packs[0]
    st.write(f"Newest pack: `{_rel(newest)}`")
    md_file = newest / "pack.md"
    html_file = newest / "pack.html"
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "Download pack.md",
            data=md_file.read_text(encoding="utf-8"),
            file_name=f"{newest.name}_pack.md",
            mime="text/markdown",
            key="download_md",
        )
    if html_file.exists():
        with col2:
            st.download_button(
                "Download pack.html",
                data=html_file.read_text(encoding="utf-8"),
                file_name=f"{newest.name}_pack.html",
                mime="text/html",
                key="download_html",
            )
else:
    st.write("No evidence packs generated yet.")
