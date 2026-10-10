# Verification: integrity fixes 1, 3, 5 (re-run after review iteration 3)

Run on 2026-10-10 from `c:\Users\hafee\agentgate` (Windows PowerShell, Python 3.11.9). I modified no source files and committed nothing. This run replaces the earlier report. It covers the current code, including the empty-angle-address fix in `attacks/judge.py` (17:08:04) and the `append_with` docstring note in `ledger/ledger.py` (17:08:08).

Overall: 8 of 8 checks PASS.

Real chain at the start: `Get-FileHash ledger\chain.jsonl` gave `040136F59BAA6165CDCD375D53F219B0A64831CDA295E813B8467496053FAC59` with 34 lines. The hash and line count were the same after the test suite, so the tests did not touch the real chain. Checks 3 to 5 then added 9 entries (indices 34 to 42) through normal CLI writes, for 43 entries in total. Final hash: `5821FC20CEE5F1514600A1205F71C35CD30BD26F30D3D1AAD31A0522AF3C4F89`. Nothing was edited by hand.

## 1. Full unittest suite: PASS

The command comes from `.agents/tasks/plan.md` line 17. The repo uses stdlib unittest, not pytest.

```
python -m unittest discover -s tests -v
```

```
Ran 90 tests in 16.049s
OK
```

Exit code 0: 90 run, 90 passed, 0 failures, 0 errors, 0 skipped. No line matched `... FAIL`, `... ERROR`, `^FAIL:`, `^ERROR:` or `skipped`. These new tests all report `ok`:

- ledger: `test_multiprocess_concurrent_appends`, `test_lock_timeout_raises_clear_error`, `test_threaded_concurrent_appends`
- gate: `test_override_replay_refused`, `test_forced_override_replay_refused`, `test_concurrent_overrides_only_one_wins`, `test_cli_override_replay_exits_nonzero`
- judge: `test_malformed_tokens_fail_closed`, which now includes the `<>` and `< >` inputs

The suite also prints a Streamlit "missing ScriptRunContext" warning. It comes from the dashboard import and is harmless.

## 2. `ledger/verify.py` on the real chain (start): PASS

```
python ledger/verify.py
```

```
OK: ledger verified (34 entries), head hash 375449b57786e8fa1e9ec808b33ceaae671878ab4716092ec5034ac078fad459
```

Exit code 0.

## 3. Offline gate decisions: PASS

| Command | Key output | Exit | Result |
|---|---|---|---|
| `python cli.py check --config configs/v1.yaml --mode offline` | `DECISION: ALLOW (threshold T = 0.15)`; gate_decision `41ba1939...` (index 35) | 0 | PASS |
| `python cli.py check --config configs/v2.yaml --mode offline` | `DECISION: BLOCK (threshold T = 0.15)`; candidate overall ASR 1.000, CI [0.963, 1.000]; top attribution row `prompt_section_removed:SECURITY +1.000 regression BLOCK`; index 37 | 1 | PASS |
| `python cli.py check --config configs/v3.yaml --mode offline` | `DECISION: ALLOW (threshold T = 0.15)`; all upper bounds <= 0.15; gate_decision `4cc730f2...` (index 39) | 0 | PASS |

## 4. Override refused when the latest decision is not REVIEW: PASS

This ran right after the v3 ALLOW from check 3.

```
python cli.py override --reason "verify: should be refused" --actor verifier
```

```
ERROR: Override refused: latest gate decision is ALLOW (entry 4cc730f2211aab4f); only REVIEW decisions can be overridden. Use --force to override anyway.
```

Exit code 1. The chain stayed at 40 lines, so nothing was written.

## 5. REVIEW overridden once, then the replay is refused: PASS

| Command | Key output | Exit |
|---|---|---|
| `python cli.py check --config configs/v3.yaml --mode offline --trials 2` | `Trials per attack: 2`, `DECISION: REVIEW (threshold T = 0.15)`, gate_decision `639193edfb998479...` (index 41) | 2 |
| `python cli.py override --reason "verify: first override" --actor verifier` | `Override recorded: index 42 entry_hash 68abe51421ed9cf8...`; `original decision REVIEW (entry 639193edfb998479) -> effective ALLOW; actor verifier; forced False` | 0 |
| `python cli.py override --reason "verify: replay attempt" --actor verifier` | `ERROR: Override refused: gate decision 639193edfb998479 (index 41) was already overridden by override entry 68abe51421ed9cf8 (index 42). Run a new check to get a new decision.` | 1 |
| `python cli.py override --reason "verify: forced replay" --actor verifier --force` | Same "already overridden" refusal. `--force` does not get past the replay check. | 1 |

The chain had 43 lines after the refused attempts, so they wrote nothing. A Python scan of `ledger/chain.jsonl` for `override` entries with `payload.decision_index == 41` returned `[42]`. Overrides per decision were `{12: 1, 23: 1, 32: 1, 41: 1}`, so no decision has more than one override.

## 6. `ledger/verify.py` on the real chain (end): PASS

```
python ledger/verify.py
```

```
OK: ledger verified (43 entries), head hash 68abe51421ed9cf82393fad9b065df9ce198edc17db354ebd001e5986ed2ff9c
```

Exit code 0.

## 7. No placeholders introduced: PASS

A workspace grep over `**/*.py` for `TODO|FIXME|XXX|NotImplementedError|raise NotImplemented|^\s*pass\s*#` found no matches.

## 8. `ledger/verify.py` unchanged: PASS

The repo has no commits, so `git diff` has nothing to compare against. I checked it two other ways:

- **Current hash and timestamp.** `Get-FileHash ledger\verify.py` gives `F76E86EADD421565FC628DE3E4887E94CBF3A95A53FD3F76DA07537E090AB98E`. Its LastWriteTime is 16:18:25. That is before the first review (16:25) and before every fix edit: `.gitignore` 16:39:23, `gate.py` 16:40:06, `judge.py` 17:08:04, `ledger.py` 17:08:08.
- **Pre-fix snapshot.** The build session's Kiro snapshot of the pre-fix file (`sess_083cda9c.../snapshots/04e7ea6d/ledger/verify.py`, 16:18:25) has the same hash, `F76E86EADD421565...`. The other six snapshots of that file are earlier build drafts (16:01:46 to 16:02:09). None are later than 16:18:25.

## Lock files and cleanup

- `ledger/chain.jsonl.lock` exists, and it is the only `.lock` file in the repo. `git check-ignore -v ledger/chain.jsonl.lock` returned `.gitignore:5:ledger/*.lock`, and the file does not appear in `git status --short --untracked-files=all`.
- No `chain.jsonl*` temp ledgers are left in `%TEMP%`. I deleted the scratch file `%TEMP%\ag_unittest.txt`.
- Side effects for the commit:
  - This run added 9 more verifier entries to the real chain, which now has 43 entries, including override index 42 by `verifier`.
  - `cache/latest_check.json` (gitignored) now shows the `--trials 2` REVIEW. Before demoing, re-run `python cli.py check --config configs/v3.yaml --mode offline` so the dashboard shows ALLOW.
