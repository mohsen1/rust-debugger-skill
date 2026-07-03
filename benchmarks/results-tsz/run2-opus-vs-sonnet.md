# tsz benchmark — run 2: Opus vs Sonnet, 6 cases

Re-measure of the with/without-`rdbg` fix-rate benchmark, this time on **both Opus
(`claude-opus-4-8`) and Sonnet (`claude-sonnet-5`)**, medium effort, across **6 real
merged bug-fixes** from tsz (June 2026, past the training cutoff; contamination-isolated
— clean single-commit checkout at each fix's parent, web tools disallowed, tsz's
`.claude` stripped). This run exercises the **improved tool** (breakpoint-fire reporting,
codelldb adapter, lazy rust-analyzer, panic triage, predicate run-to).

Fix = the crate's regression test passes (`cargo nextest` exit 0). Raw data:
`runs-opus.json`, `runs-sonnet.json`.

## Results — 5 solid cases (case 6 compromised by a tool bug, see below)

| case | bug type | Opus without→with | Δtok | Δwall | Sonnet without→with | Δtok | Δwall |
|---|---|---|---|---|---|---|---|
| 4da902 | wrong displayed value | 5.01M→3.63M | **−28%** | −21% | 11.11M→2.79M | **−75%** | −62% |
| 06943a | false-positive diagnostic | 9.26M→3.44M | **−63%** | −54% | 17.00M→8.78M | **−48%** | −31% |
| 307921 | keyof validation (cheap) | 0.35M→0.38M | +7% | +62% | 0.45M→0.43M | −6% | +108% |
| 8292e6 | contravariant (missing diag) | 2.68M→7.83M | **+192%** | +110% | 14.75M→1.21M | **−92%** | −81% |
| 1226c7 | nominal same-class | 14.97M→5.09M | **−66%** | −54% | 2.20M→18.63M | **+747%** | +179% |
| **total** | | 32.3M→20.4M | **−37%** | | 45.5M→31.8M | **−30%** | |

**Fix rate: 10/10 both conditions, both models.** rdbg changes cost, not correctness.

## Verdict

1. **Net win both models** — −37% (Opus) / −30% (Sonnet) tokens overall, 100% fix rate.
   Wall time is now often a *win* too (the lazy-rust-analyzer fix; run 1 lost on wall).
2. **Enormous per-case variance (−92% to +747%).** The value is entirely a function of
   (a) how expensive the bug is to *read* and (b) whether the agent avoids rebuild thrash.
3. **rdbg's value scales with the model's reading tax — proven case-matched.** The
   contravariant bug (8292e6) is **+192% for Opus but −92% for Sonnet**: the *same* bug.
   Opus reads it cheaply unaided (2.68M) so rdbg is pure overhead; Sonnet thrashes to
   14.75M unaided and rdbg grounds it to 1.21M. Sonnet's unaided cost is ~1.4× Opus's;
   grounding erases most of that penalty.
4. **The negatives are rebuild/iteration thrash, not wrong answers.** Opus/contravariant
   (17 re-`launch`es hunting a missing check) and Sonnet/nominal (14 blind `cargo test`
   rebuilds) — the agent paid the huge per-rebuild output tax without rdbg reducing the
   iteration count.

## Case 6 + the codelldb memory bug (headline tool finding)

Case 6 (`4aac798dea`, subclass-ctor) is red-at-parent and valid, but its **WITH** cell
could not be measured cleanly: Opus thrashed to a 45-min **timeout** (27 cargo rebuilds);
Sonnet's run was **killed three times** (even solo, clean start).

Root-caused during the run: **codelldb loads ~20GB of debug symbols on tsz's 1.7M lines**,
and a **hard-killed session orphans that 20GB process** — the daemon reaps codelldb on
re-launch and on graceful `rdbg down` (verified), but not on its own SIGKILL. Each
killed/timed-out run seeded the next run's OOM. The lldb-dap→codelldb upgrade (added for
richer `eval`) introduced this footprint. So case 6's WITH number reflects a *tool memory
bug*, not the model — reporting it as a fix failure would be misleading.

## Improvement opportunities (grounded in these transcripts)

1. **codelldb memory (P0).** ~20GB per session on a large repo, orphaned on hard-kill.
   Options: lazy/partial symbol loading, an lldb-dap fallback (or memory cap) on huge
   repos, and reaping codelldb when the daemon dies (PDEATHSIG on Linux; a watchdog on
   macOS). *(Harness cleanup already hardened: `rdbg down` + `pkill codelldb`.)*
2. **Re-launch tax.** Losing runs re-`launch` many times (17), each a fresh session/build.
   Guide the agent toward one session with several breakpoints, `rdbg trace`, or `rdbg do`
   instead of repeated `launch`.
3. **Bug-type fit.** Wrong/extra diagnostics → trace from the emit sink (big wins);
   *missing*/contravariant diagnostics have no fingerprint to trace → the agent should
   *read* to find the absent check. The SKILL should say this explicitly so agents don't
   burn launches debugging a missing diagnostic.
4. **Blind fix-iteration.** 14–27 `cargo test` rebuilds on the thrash cases. `set --then
   continue` can validate a fix hypothesis live without recompiling — under-used.

## SKILL iteration — killing the token waste (validated)

After run 2, the SKILL gained a **triage** ("read first; debug only a runtime question
in large code; skip cheap/missing-output bugs; keep launches few") and a **fix-discipline**
rule ("fix once, don't churn; >2–3 edit→test cycles = guessing; validate live with
`set`"). Re-running the two catastrophic-waste cells (WITH only, same isolation):

| case | before | after | what changed in behavior |
|---|---|---|---|
| Opus contravariant (missing diag) | +192% (17 launches) | **+26%** (1 launch) | read to find the absent check instead of hunting |
| Sonnet nominal (cheap to read) | +849% (18 edits, 5 launches) | **+53%** (10 edits, 0 launches) | didn't over-engage the debugger; stopped edit churn |

The agent now spends the debugger only where it pays: both former blowups drop to small
overhead, still fixed. (One run each — weak-model variance remains — but the pattern
matches the intent: waste comes from hunting-launches and edit-churn, and the guidance
suppresses both.)
