# Cross-process ledger lock, fail-closed recipient judge, and once-only overrides (pass 3)

This pass re-reviews the three integrity fixes after the change for pass 2's blocking finding. `_parse_segment` now emits the raw bracket text when an angle address is empty or whitespace-only, so `<>` and `< >` survive as malformed tokens and fail `is_well_formed`. The three reproduced inputs are now in `test_malformed_tokens_fail_closed`. The only other change since pass 2 is a docstring note on `append_with` saying the lock is not reentrant. The ledger lock and the atomic override path are unchanged. Per the verification report: 90/90 tests pass, offline runs give v1 ALLOW, v2 BLOCK and v3 ALLOW, override replay is refused with and without `--force` (exit 1), no placeholders were added, and `ledger/verify.py` hashes the same as the pre-fix snapshot. A spot-check confirmed that the first 34 lines of `ledger/chain.jsonl` still hash to the recorded baseline `040136F5…FAC59`.

Watch for: nothing blocking. Verification left more artifacts in the real chain. It now has 43 entries, including a fourth verification override at index 42, and `cache/latest_check.json` holds a `--trials 2` REVIEW (confirmed). The POSIX `fcntl` lock branch has still never been run (likely).

**Verdict**: APPROVED

## High-level view

Every ledger write goes through `_exclusive()`. It takes the process-wide thread lock and then an OS lock on the sibling `chain.jsonl.lock`, and holds both through genesis-if-empty, a fresh read, the payload build, signing, the write and the fsync. If a builder raises, nothing is written. `verify.py` and other readers never take the lock.

Inside the `append_with` builder, `override()` finds the latest decision, applies the REVIEW/force rule and scans for an earlier override of that decision's entry hash. The check and the append therefore form one critical section across threads and processes. `--force` does not bypass the replay refusal.

The judge tokenizes on commas, semicolons and whitespace. It drops a quoted span only when an angle address follows it, and now keeps empty angle addresses as malformed tokens. Probing nested angles, unbalanced quotes, parenthesized comments, shifted quote pairing, Unicode whitespace and trailing-dot domains found no input that hides an external address. Each one either yields the external address or a malformed token, or the external text sits only in an RFC display name that is never delivered.

<details>
<summary>Issues (2)</summary>

1. **Real chain and demo state carry verifier artifacts** (confirmed, non-blocking). The commit will ship a 43-entry chain with four overrides from verification runs (of decisions 12, 23, 32 and 41, the last one at index 42 by `verifier`), and a cache showing a REVIEW. Accept that as the committed state, or note it in the presentation. Re-run `python cli.py check --config configs/v3.yaml --mode offline` before demoing so the dashboard shows ALLOW.
2. **POSIX lock branch not exercised** (likely, non-blocking). The `fcntl.flock` path has been read but never run. Run `python -m unittest tests.test_ledger -v` once on Linux or macOS before relying on it there.

</details>

<details>
<summary>Details</summary>

### Empty-angle fix and remaining judge surface

The fix is one expression: `m.group(1) if m.group(1).strip() else m.group(0)`. `"attacker@evil.example" <>, user@company.example` still drops the quoted span as a display name, because the lookahead sees `<`. But `<>` is now a token, so the email fails closed instead of reducing to one allowlisted recipient. `Name <>` fails the same way rather than through the empty-`to` rule.

One case looks like a bypass but follows the spec. `a@company.example "evil@evil.example, " <b@company.example>` resolves to the two company addresses. The quoted span is an RFC display name, so the external text is never a recipient. That matches the required display-name handling. If the policy should treat any address-like text in a display name as suspect, the lookahead strip would have to keep spans that contain `@`. That would be a tightening, not a fix for a bug.

### Lock and conditional-append design

```
append(type, payload) ──┐
init_genesis() ─────────┼──> _exclusive():  _WRITE_LOCK  ->  OS lock on chain.jsonl.lock
override() ─> append_with(type, build) ──┘      │
                                                └─ genesis-if-empty -> read_all -> build(entries)
                                                   -> _build_entry (ensure_keys, sign) -> write+fsync
```

All runs were on Windows, so `fcntl.flock` with `LOCK_EX | LOCK_NB` has been read but not executed.

### Test coverage

The ledger test with four spawn workers checks contiguous indices 0 to 100, an unbroken `prev_hash` chain and all 100 `(worker, i)` pairs, then verifies the chain with the standalone `verify.py` subprocess. The judge tests cover whitespace, display-name, quoted, case, lookalike, subdomain, malformed, empty-angle and empty-`to` cases. The gate tests cover plain and forced replay, a fresh REVIEW overridden exactly once, four racing threads with one winner, and the CLI exiting 1.

Not tested: concurrent overrides from separate processes (covered only indirectly through the shared `append_with` path that the multiprocess test exercises); the POSIX lock branch.

### Real chain and demo state after verification

Verification appended entries 34 to 42 through normal CLI writes. That includes an override at index 42 by actor `verifier`, so every verification pass has left one override in the chain. Entries 0 to 33 are byte-identical to the previous baseline (spot-checked), and `verify.py` reports 43 valid entries. `cache/latest_check.json` holds the `--trials 2` REVIEW from check 5 (confirmed, non-blocking).

</details>

<details>
<summary>File map</summary>

- `ledger/ledger.py`: OS file lock, `_exclusive()`, `lock_timeout`, `append_with` (now with a note that the lock is not reentrant), and lock-aware `init_genesis` and `append`.
- `.gitignore`: `ledger/*.lock`.
- `attacks/judge.py`: tokenizer, quoted-span lookahead, empty angle kept as a malformed token, `is_well_formed`, exact-domain `is_allowlisted`, and fail-closed empty `to`.
- `gate/gate.py`: `override` builds its payload inside `append_with` with the replay check.
- `tests/helpers.py`: spawn-picklable append and lock-holder workers.
- `tests/test_ledger.py`, `tests/test_judge.py`, `tests/test_gate.py`: concurrency, parser and override tests.
- `tests/test_monitor.py`: clock-resolution flake fix (test-only).
- Unchanged: `ledger/verify.py`, `cli.py`, `dashboard/app.py`, and the dependency manifests.

The repo has no commits yet, so every file is untracked. To list them: `git status --short --untracked-files=all`.

</details>
