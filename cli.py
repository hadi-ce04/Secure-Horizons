"""AgentGate command-line interface.

Run from the repository root with ``python cli.py <command>`` (or ``agentgate
<command>`` after ``pip install -e .``).

Commands: init, check, verify, pack, override, monitor.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import click  # noqa: E402

from agent.runner import OllamaUnavailableError  # noqa: E402
from evidence.pack import generate_pack  # noqa: E402
from gate.gate import DEFAULT_BASELINE, EXIT_CODES, GateError, override, run_check  # noqa: E402
from gate.run import DEFAULT_CONCURRENCY, DEFAULT_TRIALS_PER_ATTACK  # noqa: E402
from ledger.ledger import LedgerError, get_default_ledger  # noqa: E402
from monitor.schedule import (  # noqa: E402
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MAX_AGE_HOURS,
    DEFAULT_MONITOR_CONFIG,
    run_monitor_loop,
)

EXIT_ERROR = 3
VERIFY_SCRIPT = REPO_ROOT / "ledger" / "verify.py"
DEFAULT_CHAIN = REPO_ROOT / "ledger" / "chain.jsonl"
DEFAULT_PUBLIC_KEY = REPO_ROOT / ".keys" / "public.pem"


def _print_table(headers: list[str], rows: list[list]) -> None:
    """Print a fixed-width ASCII table."""
    cells = [[str(h) for h in headers]] + [[str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    click.echo(sep)
    click.echo("| " + " | ".join(cells[0][i].ljust(widths[i]) for i in range(len(headers))) + " |")
    click.echo(sep)
    for row in cells[1:]:
        click.echo("| " + " | ".join(row[i].ljust(widths[i]) for i in range(len(headers))) + " |")
    click.echo(sep)


def _summary_rows(summary: dict) -> list[list]:
    rows = []
    items = list(summary["categories"].items()) + [("overall", summary["overall"])]
    for cat, s in items:
        rows.append(
            [
                cat,
                s["n"],
                s["successes"],
                f"{s['asr']:.3f}",
                f"[{s['ci_lower']:.3f}, {s['ci_upper']:.3f}]",
                f"{s['agreement']:.3f}",
            ]
        )
    return rows


def _default_actor() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


@click.group(help="AgentGate: statistical security release gate for LLM agent configs with a signed ledger.")
def cli() -> None:
    """Root command group."""


@cli.command("init", help="Create the Ed25519 keypair (if missing) and the ledger genesis entry.")
def init_cmd() -> None:
    try:
        ledger = get_default_ledger()
        ledger.ensure_keys()
        genesis = ledger.init_genesis()
    except LedgerError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(EXIT_ERROR)
    click.echo(f"Private key: {ledger.private_key_path}")
    click.echo(f"Public key:  {ledger.public_key_path}")
    click.echo(f"Chain:       {ledger.chain_path}")
    click.echo(f"Genesis entry hash: {genesis['entry_hash']}")


@cli.command(
    "check",
    help=(
        "Run the attack suite on baseline and candidate configs and apply the gate. "
        "Exit codes: 0 ALLOW, 1 BLOCK, 2 REVIEW, 3 error (Click usage errors also exit 2)."
    ),
)
@click.option("--config", "config_path", required=True, help="Candidate config YAML.")
@click.option("--baseline", "baseline_path", default=DEFAULT_BASELINE, show_default=True, help="Baseline config YAML.")
@click.option("--mode", type=click.Choice(["offline", "ollama"]), default="offline", show_default=True)
@click.option("--trials", type=click.IntRange(min=1), default=DEFAULT_TRIALS_PER_ATTACK, show_default=True,
              help="Trials per attack.")
@click.option("--concurrency", type=click.IntRange(min=1), default=DEFAULT_CONCURRENCY, show_default=True)
@click.option("--no-cache", is_flag=True, help="Ignore and do not write the trial cache.")
@click.option("--ollama-model", default=None, help="Override the Ollama model name.")
@click.option("--no-attribution", is_flag=True, help="Skip per-change attribution.")
def check_cmd(config_path, baseline_path, mode, trials, concurrency, no_cache, ollama_model, no_attribution) -> None:
    try:
        result = run_check(
            config_path,
            baseline_path=baseline_path,
            mode=mode,
            trials_per_attack=trials,
            concurrency=concurrency,
            use_cache=not no_cache,
            ollama_model=ollama_model,
            run_attribution=not no_attribution,
        )
    except (OllamaUnavailableError, FileNotFoundError, ValueError, LedgerError, RuntimeError) as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(EXIT_ERROR)

    b, c = result["baseline"], result["candidate"]
    click.echo(f"Baseline:  {b['name']} v{b['version']} ({b['path']}) hash {b['hash'][:16]}")
    click.echo(f"Candidate: {c['name']} v{c['version']} ({c['path']}) hash {c['hash'][:16]}")
    click.echo(f"Mode: {result['mode']}  Model: {result['model']}  Trials per attack: {result['trials_per_attack']}")
    headers = ["category", "n", "successes", "ASR", "95% CI [lo, hi]", "agreement"]
    click.echo("")
    click.echo("Baseline results:")
    _print_table(headers, _summary_rows(b["summary"]))
    click.echo("Candidate results:")
    _print_table(headers, _summary_rows(c["summary"]))

    decision = result["decision"]
    click.echo("")
    click.echo(f"DECISION: {decision['decision']} (threshold T = {decision['threshold']:.2f})")
    for reason in decision["reasons"]:
        click.echo(f"  - {reason}")

    rows = result["attribution"]["rows"]
    click.echo("")
    if rows:
        click.echo("Attribution (variant = baseline + one change, sorted by impact):")
        _print_table(
            ["change", "kind", "delta overall", "effect", "variant decision"],
            [[r["change_id"], r["kind"], f"{r['delta_overall']:+.3f}", r["effect"], r["variant_decision"]] for r in rows],
        )
    elif no_attribution:
        click.echo("Attribution: skipped (--no-attribution).")
    else:
        click.echo("Attribution: no changes between baseline and candidate.")
    click.echo(f"Ledger: check_run {result['ledger']['check_entry_hash'][:16]}, "
               f"gate_decision {result['ledger']['decision_entry_hash']} (index {result['ledger']['decision_index']})")
    if decision["decision"] == "REVIEW":
        click.echo("REVIEW requires a signed manual override: python cli.py override --reason \"<justification>\"")
    sys.exit(EXIT_CODES[decision["decision"]])


@cli.command("verify", help="Verify the ledger with the standalone ledger/verify.py auditor (exit code propagated).")
@click.option("--chain", "chain_path", default=None, help="Chain JSONL path (default ledger/chain.jsonl).")
@click.option("--key", "key_path", default=None, help="Public key PEM path (default .keys/public.pem).")
def verify_cmd(chain_path, key_path) -> None:
    args = []
    if chain_path or key_path:
        args.append(str(chain_path or DEFAULT_CHAIN))
    if key_path:
        args.append(str(key_path))
    proc = subprocess.run([sys.executable, str(VERIFY_SCRIPT), *args], capture_output=True, text=True)
    if proc.stdout:
        click.echo(proc.stdout.rstrip())
    if proc.stderr:
        click.echo(proc.stderr.rstrip(), err=True)
    sys.exit(proc.returncode)


@cli.command("pack", help="Generate a Markdown + HTML evidence pack from the latest check (runs one if needed).")
@click.option("--config", "config_path", default=None, help="Candidate config (re-runs check if it differs from latest).")
@click.option("--baseline", "baseline_path", default=None, help="Baseline config (re-runs check if it differs).")
@click.option("--mode", type=click.Choice(["offline", "ollama"]), default="offline", show_default=True)
@click.option("--trials", type=click.IntRange(min=1), default=DEFAULT_TRIALS_PER_ATTACK, show_default=True)
def pack_cmd(config_path, baseline_path, mode, trials) -> None:
    try:
        res = generate_pack(candidate_path=config_path, baseline_path=baseline_path, mode=mode, trials_per_attack=trials)
    except (OllamaUnavailableError, FileNotFoundError, ValueError, LedgerError, RuntimeError) as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(EXIT_ERROR)
    click.echo(f"Evidence pack ({res['decision']}): {res['pack_dir']}")
    click.echo(f"  pack.md   {res['md_path']}  sha256 {res['sha256']['pack.md']}")
    click.echo(f"  pack.html {res['html_path']}  sha256 {res['sha256']['pack.html']}")
    click.echo(f"Ledger: pack_generated {res['ledger_entry_hash']}")


@cli.command("override", help="Record a signed manual override of the latest REVIEW gate decision.")
@click.option("--reason", required=True, help="Justification for the override (required, non-empty).")
@click.option("--actor", default=None, help="Who is overriding (default: current OS user).")
@click.option("--force", is_flag=True, help="Allow overriding a non-REVIEW decision (recorded as forced).")
def override_cmd(reason, actor, force) -> None:
    try:
        entry = override(reason, actor or _default_actor(), force=force)
    except (GateError, LedgerError) as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)
    p = entry["payload"]
    click.echo(f"Override recorded: index {entry['index']} entry_hash {entry['entry_hash']}")
    click.echo(f"  original decision {p['original_decision']} (entry {p['decision_entry_hash'][:16]}) -> "
               f"effective {p['effective_decision']}; actor {p['actor']}; forced {p['forced']}")
    click.echo(f"  signature {entry['signature']}")


@cli.command("monitor", help="Re-run the suite on a schedule and raise staleness/drift alerts.")
@click.option("--config", "config_path", default=DEFAULT_MONITOR_CONFIG, show_default=True)
@click.option("--interval", type=click.FloatRange(min=0), default=DEFAULT_INTERVAL_SECONDS, show_default=True,
              help="Seconds between runs.")
@click.option("--once", is_flag=True, help="Run a single pass and exit.")
@click.option("--iterations", type=click.IntRange(min=1), default=None, help="Stop after N runs.")
@click.option("--max-age-hours", type=click.FloatRange(min=0), default=DEFAULT_MAX_AGE_HOURS, show_default=True)
@click.option("--mode", type=click.Choice(["offline", "ollama"]), default="offline", show_default=True)
@click.option("--trials", type=click.IntRange(min=1), default=DEFAULT_TRIALS_PER_ATTACK, show_default=True)
@click.option("--ollama-model", default=None)
def monitor_cmd(config_path, interval, once, iterations, max_age_hours, mode, trials, ollama_model) -> None:
    try:
        run_monitor_loop(
            config_path=config_path,
            interval_seconds=interval,
            iterations=1 if once else iterations,
            printer=click.echo,
            mode=mode,
            trials_per_attack=trials,
            max_age_hours=max_age_hours,
            ollama_model=ollama_model,
        )
    except KeyboardInterrupt:
        click.echo("Monitor stopped.")
    except (OllamaUnavailableError, FileNotFoundError, ValueError, LedgerError, RuntimeError) as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(EXIT_ERROR)
    sys.exit(0)


def main() -> None:
    """Console-script entry point."""
    cli()


if __name__ == "__main__":
    main()
