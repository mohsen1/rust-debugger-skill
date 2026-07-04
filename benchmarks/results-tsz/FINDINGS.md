# tsz benchmark — consolidated findings

The complete arc of "does giving a coding agent a debugger (`rdbg`) help it fix real bugs
in a large Rust codebase, and if so, why." Contamination-isolated cases (real merged
tsz bug-fixes, clean single-commit checkout at each fix's parent, web disallowed).
Detailed run docs are linked at the bottom; this is the summary of record.

## Bottom line (read this first)

- **Fix rate never differs.** With or without the debugger, the agent fixes the bug —
  every scale we tested (3/3, 22-case, 60/60 across the sibling Go study). The debugger
  changes **cost**, never **capability**.
- **The value variable is read-localization cost**, not repo size: big token wins only
  where the deciding code is expensive to *find by reading*; ~neutral or negative where a
  read already pins it.
- **On closed code, the win is the SKILL's approach-guidance — not launching the
  debugger.** Across the full multi-model/effort matrix there is **no cell where debugger
  *execution* drove a win**; every token win came at **0 launches** (the prompt working),
  and every cell that actually launched rdbg on a hard case **thrashed**.
- **The one clean tool value is variance-narrowing** (the debugger makes a weak model more
  *consistent*), confirmed across two vendors.
- **Implication:** tsz is a *closed* system — all facts are latent in the code, just
  expensive to derive — so a better prompt that reads well always matches or beats running
  the code. The debugger/observation layer can only prove it does something a prompt can't
  on an **open system where the deciding fact isn't in any file** (cross-service contracts,
  flag/tenant splits, workflow reality). That's the motivation for the thirdface explorer.

## The runs

| run | scope | headline |
|---|---|---|
| **run 1** | 3 cases, Opus | first signal: −41% aggregate; wins scale with read-difficulty |
| **run 2** | 6 cases, Opus vs Sonnet | exposed the waste patterns (contravariant **+192%**, nominal **+747%**) and the per-model divergence (same bug: Opus +192%, Sonnet −92%) |
| **run 3** | **22 cases, Opus, revised SKILL** | −47% aggregate (86.9M→46.2M), median −29%, **100% fix**, **no systematic +100% waste**; the SKILL triage flipped run-2's disasters into wins |
| **run 4** | multi-model × effort, N≥3, multi-trial | **the crux**: the wedge is a *prompt* effect (0 launches), doesn't transfer to Codex, and the clean tool value is variance-narrowing |

### run 3 — the sweep (SKILL triage kills systematic waste)
22 contamination-isolated cases. Aggregate **−47%** tokens, median **−29%**, fix rate
**32/32**. The "triage + fix-discipline" SKILL turned the run-2 blowups into wins
(contravariant +192%→−82%). Residual positives were small cheap-case overhead; the two
apparent +100% cells (`b01338524f`, nominal) came back **−5%** and **−39%** at the
multi-trial median — i.e. **variance, not systematic waste**. Single-run variance is high
(a cell measured 3.4M–16.2M across runs), so only medians are trustworthy.

### run 4 — the wedge, and the crux
The "cheap model + rdbg ≈ frontier model" claim, pressure-tested:
1. **Wedge holds at the median** — Sonnet+condition **1.38M ≤ Opus-unaided 1.70M** on the
   contravariant bug — **but it's a prompt effect: Sonnet launched rdbg 0× in all winning
   trials.** The SKILL's "read for the absent check, don't hunt" guidance fixed its
   *reading approach*; the debugger wasn't run. Distributions overlap; only medians split.
2. **Does not transfer to Codex** — gpt-5.5 reads the same bug cheaply unaided (1.28M, like
   Opus) and *declines* rdbg (0 launches in 7/9 WITH). The wedge is a property of a **weak
   reader that thrashes**, not a universal law.
3. **Effort sweep reinforces the crux** — Sonnet's only win across low/med/high is medium
   (0 launches); at low AND high effort it *launched* rdbg and thrashed to **+1354% /
   +1371%**. Launching the debugger made the weak reader *worse* at every effort except the
   one where it didn't launch.
4. **Variance-narrowing is the clean tool value** — Sonnet ×10.8→×5.5, codex-low
   ×29.4→×1.7. The debugger makes the weak model more consistent even when the median
   barely moves.

**The crux, over all 12 cells: zero counter-cells — no cell where rdbg *execution* drove a
win.** Every win = 0 launches (skill); every ≥2-launch hard-case cell failed to win.

## Mechanism study (87 transcripts) — *how* it wins, when it wins

Parsed all completed tsz+uv transcripts; read the 12 WITH-runs that actually launched rdbg.

- **Cost is debugger *discipline*, not the bug.** Cheap wins averaged **~4 rdbg calls**;
  expensive runs averaged **~45**. `step` appears only in the two most expensive runs.
- **The killer comparison — same bug, opposite outcome:** on the contravariant bug, Sonnet
  did **2 calls → 1.2M tokens (win)**; Opus did **92 calls → 10.5M (thrash)**. Both fixed
  it; the difference was purely *how much they debugged*.
- **The winning recipe = "tap, don't walk":** break at the **sink** (where the wrong result
  surfaces), read **which path fired / which breakpoints did NOT fire**, `bt` to the
  deciding code, then **read that code**. The losing pattern is walking execution (stepping,
  eval-loops, `dbg!`/`println!` instrumentation). This is now baked into the shipped SKILL.

## Theater study (36 independent judges, majority vote) — the grounding rate

Classified each of the 12 WITH-rdbg trajectories as causal / mixed / theater.

- **8/12 theater, 2/12 causal, 2/12 mixed** — ~83% of debugger use is non-causal
  (replicates the sibling Go study's "75–93% decorative" and the rlenv's 1/6). Naive
  trajectory harvesting would train grounding-as-performance.
- **The twist: theater ≠ worthless.** The Sonnet wedge *win* is judged **theater** (0 launch
  → the debugger didn't determine the fix). Its value came from **one** observation —
  `push_diagnostic — bound, 0 hits` (the sink was never hit) — which **aimed the reading**
  so Sonnet read the right 3 functions instead of thrashing. **The value is
  localization-narrowing, not fix-causality.**
- **The genuine runtime-only signal is the hit-count / never-hit primitive**, not
  interactive `eval`/`step`. Confabulation concentrates on the *hardest* bug (`4aac`, 2/3
  judges flagged invented observations) — the model manufactures evidence when it's stuck.

## Honest caveats

- Single-run variance is high; only multi-trial medians are quoted for the fine claims.
- Everything here is **Opus/Sonnet + one Codex model** on **one repo (tsz)** plus a second
  (uv) for generalization. uv confirmed the "restraint generalizes; the win is
  localization-gated" boundary: 0/11 rdbg launches (bugs were legible/self-locating).
- The debugger's *real* value proposition — facts not derivable by reading — is **untestable
  on closed code by construction.** That is the finding, not a gap.

## Detailed docs & artifacts

- Method + canonical overview: `benchmarks/results-tsz/README.md`
- run 2 (Opus vs Sonnet): `benchmarks/results-tsz/run2-opus-vs-sonnet.md`
- run 3 (22-case sweep): `benchmarks/results-tsz/run3-full-sweep.md`
- run 4 (multi-model/effort wedge — on branch `feat/multimodel-codex`, not merged):
  `~/code/rdbg-multimodel/benchmarks/results-tsz/run4-multimodel-wedge.md` +
  interactive artifact: https://claude.ai/code/artifact/8e890822-0c46-4db0-a589-65f2139587e6
- Mechanism + theater study artifacts: session scratchpad `study/` (corpus_metrics.json,
  narratives/, theater judge results) — not yet promoted to a repo doc.
