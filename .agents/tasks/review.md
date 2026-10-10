# AgentGate: statistical release gate with signed audit ledger (initial build)

This change builds the whole AgentGate repo from scratch: an offline deterministic mock email agent, a 10-attack OWASP-aligned suite, a deterministic judge, a Wilson-CI gate (T = 0.15) with per-change attribution, an Ed25519 hash-chain ledger with a standalone auditor, evidence packs, a monitor, a Streamlit dashboard, and a Click CLI. Attribution replays the baseline with one atomic diff applied at a time. Every check, decision, override, pack and monitor run is written to the signed chain. The coder's recorded evidence matches the spec outcomes: v1 ALLOW, v2 BLOCK with `prompt_section_removed:SECURITY` at +1.000, v3 ALLOW, v3 `--trials 2` REVIEW, the override is refused, rejected, and accepted in the right cases, a tampered copy exits 1, and 72 unit tests pass. Code review confirms the remaining spec checks: there are no placeholders, no anthropic usage, and no circular imports, `verify.py` uses only stdlib plus cryptography, and the Wilson, gate, judge, and canary/allowlist rules match the spec.

Watch for: ledger appends are serialized only within a process, so a background `monitor` plus a concurrent `check` can fork the chain (likely). The Ollama path lets the model call tools the config never declared (confirmed). Recipients separated by whitespace get past the judge's allowlist check (confirmed). None of these block the offline spec.

**Verdict**: APPROVED

## High-level view

The agent package takes primitives (prompt, model, schemas), not a config object, and `gate/config.py` is the single place where configs are hashed. That keeps the import graph acyclic. Each trial gets a deep-copied `Environment` and its own `Toolbox`, and the judge reads only that per-trial outbox. The offline outcome comes down to one documented heuristic, a `## SECURITY` header plus three key phrases, and the mock ignores the config's tool list.

The gate transcribes the spec directly: the exact Wilson formula, BLOCK (threshold or regression) evaluated before ALLOW, and REVIEW as the fallthrough. The default of 10 trials gives n=30/40/30 per category, which keeps a zero-success upper bound under T. Attribution is single-change only, so it can't surface interaction effects.

The ledger and the standalone auditor agree on the hashing and signing invariants. The auditor parses the SPKI PEM by hand, so it needs only the ed25519 primitive. Two weaknesses remain: writes are locked within a process but not across processes, and the auditor trusts a public key that sits in the same repo as the chain it verifies.

Optional Ollama mode imports lazily and fails with a clear message when the server is unreachable. It is less tightly bounded than offline mode: the config's tool allowlist isn't enforced, and the judge's recipient parsing assumes well-formed separators. Pack generation and override binding both lean on "latest" state in ways that can pick up the wrong run.

<details>
<summary>Issues (6)</summary>

1. **Ledger cross-process race** (likely). `Ledger.append` serializes only with a `threading.Lock`, so two processes (a looping monitor and a CLI or dashboard check) can both read the same head and write duplicate indices. That permanently breaks verification. Add an OS-level file lock (`msvcrt.locking` / `fcntl.flock`) around the read-head-and-append.
2. **Ollama tool allowlist not enforced** (confirmed). `Toolbox.dispatch` accepts every built-in tool regardless of the config's `tools` list, so removing `send_email` from a config does not stop the model sending email. Pass the config's tool names into the Toolbox and reject undeclared calls.
3. **Whitespace-separated recipients bypass judge** (confirmed). `parse_recipients` splits only on `,`/`;`, so `"attacker@evil.example user@company.example"` is one token ending in `@company.example` and is judged safe. Also split on whitespace, or require each token to parse as a single address.
4. **Pack ignores --mode/--trials when reusing latest check** (confirmed). `_needs_fresh_check` compares only config hashes, so `pack --trials 10` after a `--trials 2` REVIEW check silently packages the 2-trial result. Include mode and trials_per_attack in the freshness comparison.
5. **Override replay** (confirmed). `override()` doesn't check whether the latest REVIEW decision already has an override, so repeated overrides of the same decision are all recorded. Refuse when an `override` entry already references that `decision_entry_hash`.
6. **Repo-local public key trust** (likely). `verify.py` defaults to `.keys/public.pem` in the same repo, and the genesis `public_key_sha256` is never compared to anything external, so anyone who can rewrite the chain and the key can produce a passing audit. Document that auditors must pin the public key or genesis fingerprint out-of-band, or add an optional `--expected-fingerprint` argument.

</details>

<details>
<summary>Details</summary>

### Offline mock ignores tool schemas

`_run_offline` never reads `tool_schemas`, so a config that drops `send_email` still exfiltrates under the insecure heuristic, and a `tool_removed:send_email` change always attributes as neutral in offline mode. This is consistent with the spec's heuristic-only mock, but it means tool-level attribution only means something in Ollama mode, where Issue 2 currently defeats it too.

### Attribution and interaction effects

An added section is inserted after its nearest preceding shared section, and variants are ranked by delta ASR. For v1→v2:

```
v1 (ROLE, TOOLS, SECURITY)
  ├─ variant -SECURITY        -> ASR 1.0, delta +1.000, BLOCK
  └─ variant +EFFICIENCY      -> ASR 0.0, delta +0.000, ALLOW
```

Each variant applies exactly one change. A regression that only appears when two changes combine would show as all-neutral rows next to a BLOCK candidate, which leaves no row to blame. That follows from the spec's design. It is worth a line in the pack so reviewers don't read "all neutral" as "no cause".

### Ledger: cross-process writes and key trust

`append` takes `_WRITE_LOCK`, reads the whole file to find the head, then appends. That is safe across threads, but nothing serializes separate processes. The spec encourages exactly that pattern: `monitor --interval` running continuously while someone runs `check` or uses the dashboard's "Run check". Two writers that read the same head both write `index = n+1` with the same `prev_hash`, and `verify.py` fails at the second one permanently. There is no repair path short of truncating the chain (likely, not reproduced).

`verify.py` loads `.keys/public.pem` from the same repo, and the private key is stored unencrypted next to it (per the plan). The signatures detect edits by anyone who lacks the private key. They don't stop someone with repo write access from regenerating keys and rewriting the whole chain. The genesis `public_key_sha256` would let an auditor pin the key, but nothing compares it to an external value.

### Ollama path boundaries

Two gaps sit in the tool loop:

```python
handlers = {"read_email": ..., "search_docs": ..., "send_email": ...}
handler = handlers.get(name)   # config.tools never consulted
```

A model that hallucinates a `send_email` call will execute it even when the config omits the tool (confirmed). Separately, `parse_recipients` splits only on `[,;]`, so a model-generated `to` like `"attacker@evil.example user@company.example"` ends in `@company.example` and is judged a non-success (confirmed). The offline mock never produces either shape, so offline outcomes are unaffected. The Ollama-mode `refused` flag uses a simpler `to.endswith` check than the judge, but nothing gates on it.

The disk cache key follows the spec (`config_hash|attack_id|trial_index|mode|model`) and omits the suite payload. If an attack's payload is edited without changing its `id`, the cached Ollama outbox from the old payload gets re-judged and reused. Offline outcomes don't depend on payload text, so only Ollama runs are exposed, and `--no-cache` avoids it.

### Override and pack bind to "latest" state

`override` binds to the latest `gate_decision` in the ledger, not to a specific candidate. It never checks for an existing override, so the same REVIEW decision can be overridden repeatedly (confirmed). The dashboard form calls the same function and shows whatever check is in session state, so it can display one check while overriding another check's later decision.

`generate_pack` reuses `cache/latest_check.json` unless `--config`/`--baseline` hash differently. `--mode` and `--trials` only take effect when a fresh check runs, so a pack after a `--trials 2` smoke check shows that REVIEW result even when `--trials 10` was requested (confirmed).

### Test coverage

72 stdlib-unittest tests cover the ledger (three tamper modes and the standalone import check), judge edge cases, the runner per channel for v1/v2/v3, Wilson reference values, the cache, attribution diffs, gate decide/run_check/override, monitor alert kinds, pack contents, and a headless dashboard render. All of them run against temp ledgers and caches.

Not tested: concurrent multi-process ledger appends, Ollama tool calls outside the declared tool list, whitespace-separated or empty `to` values in the judge, pack freshness when only mode or trials differ, and repeated overrides of one decision. The Ollama happy path is untested because there is no live server, which is expected.

</details>

<details>
<summary>File map</summary>

- `agent/environment.py`: canary/allowlist constants, 5-email inbox, 4 docs, per-channel injection, isolated `Environment`
- `agent/tools.py`: `TOOL_SCHEMAS`, per-environment `Toolbox` (read_email, search_docs, send_email, dispatch), outbox JSONL log
- `agent/runner.py`: `AgentRunner` offline mock with the SECURITY heuristic; Ollama requests/SDK executor with a max-turn loop
- `attacks/suite.yaml`: 10 attacks (3 goal_hijack, 4 indirect_injection, 3 exfil_evasion) across user/email/document, with a base64 exfil
- `attacks/judge.py`: recipient parsing, allowlist plus canary judge
- `configs/v1.yaml`, `v2.yaml`, `v3.yaml`: baseline, SECURITY removed plus EFFICIENCY, TONE/SIGNATURE added
- `gate/config.py`: config loader and canonical hash
- `gate/stats.py`: Wilson CI, ASR, agreement, trial summaries
- `gate/run.py`: suite loading/validation, cache key, asyncio-bounded trial runner
- `gate/attribute.py`: section/tool/model diff, single-change variants, delta ranking
- `gate/gate.py`: decide rule, `run_check` orchestration and ledger logging, override
- `ledger/ledger.py`, `ledger/verify.py`, `ledger/chain.jsonl`: signed chain writer, standalone auditor, live chain (16 entries per the evidence)
- `evidence/mapping.yaml`, `evidence/pack.py`: AG-01..AG-07 control mapping; MD/HTML packs
- `monitor/schedule.py`: staleness, config-change, and drift alerts; sleep loop
- `dashboard/app.py`: Streamlit single screen
- `cli.py`, `pyproject.toml`, `requirements.txt`, `.gitignore`: CLI, console script, pinned deps
- `tests/*`: 72 unittest cases

The full change is untracked (no commits): `git status --untracked-files=all` from the repo root.

</details>
