# What the benchmarks actually showed

Two questions: does rdbg help an agent, and will an agent use it?

## Tier 1 — small planted-bug crates
With rdbg available, agents dip into it and it modestly helps on bugs that need a
runtime value (see `bench.py` results). On bugs you can spot by reading, it's a wash.

## Tier 2 — real fixed bugs in tsz (~1.7M lines)
Every case the mining surfaced was a diagnostic/display bug, and across every run
the agent used rdbg **0 times** — it grepped the emit code and fixed it. The
fingerprint-trace *works* (break `--fn push_diagnostic`, `eval diag.code`, `bt`
walks back to the deciding function), but with a neutral prompt the agent never
reached for it, even when the exact recipe was in the prompt.

## The adoption experiment (the answer)
Same failing test, Claude Code / Opus / medium effort, 10 runs:

| | strong CLAUDE.md | control |
|---|---|---|
| used rdbg | 5/5 (100%) | 0/5 (0%) |
| mean rdbg calls | 7.6 | 0 |
| mean tokens | 386k | 135k |
| mean wall | ~67s | ~24s |
| passed | 5/5 | 5/5 |

**Prompting fully controls adoption (0% → 100%).** The Read/Grep/Run bias is a
default, not a constraint — a forceful CLAUDE.md that mandates the debugger and
discourages the grep loop flips it completely.

**But adoption ≠ benefit.** On this readable bug, forcing rdbg cost ~2.85x tokens
and ~2.8x wall for zero correctness gain. The debugger is overhead when reading
already works.

## Takeaway
Don't mandate rdbg blanket — that's ~3x waste on easy bugs. Trigger it
selectively: when the bug is runtime-opaque or the agent is stuck in a
non-converging read loop. rdbg's value is real but conditional on the bug
actually needing runtime state; a passive "skill available" note yields 0 use, so
adoption needs an opinionated skill/hook, tuned to fire when it will pay off.

## The ROI test — does rdbg pay off on a *hard* bug?

A runtime-opaque bug (RPN calculator, swapped operands on non-commutative ops —
the wrong final value doesn't point at the fault). 10 runs, Opus, medium effort:

| | strong | control |
|---|---|---|
| used rdbg | 4/5 | 0/5 |
| passed | 5/5 | 5/5 |
| mean tokens | 278k | 153k (**1.82x cheaper**) |
| mean wall | 52.5s | 25.7s |

Even here rdbg did **not** pay off: the plain read loop matched its perfect pass
rate at ~half the tokens and half the wall. The penalty did shrink vs the easy
bug (2.85x → 1.82x), so rdbg is *relatively* less wasteful as bugs get harder —
but it never crosses into positive. Telling: the one strong run that skipped
rdbg was the cheapest strong run and matched control, while the four that used it
averaged ~310k tokens — invoking the debugger itself roughly doubled cost with no
upside.

## Bottom line

1. **Adoption is fully controllable by prompting** (0% → ~100% with a forceful
   CLAUDE.md). The Read/Grep/Run bias is a default, not a wall.
2. **But at small/medium Rust scale, a debugger is a net cost for autonomous
   agents** — Opus reads code well enough that reading is cheaper and equally
   reliable. Forcing rdbg adds 1.8–2.9x tokens for no correctness gain.
3. **The gap narrows with difficulty**, which points at where rdbg *should* win:
   situations reading genuinely can't resolve — panics with unclear cause,
   data/concurrency-dependent heisenbugs, or very large codebases where reading is
   expensive — plus human and confirmation use (the #15366 fix). Not everyday
   small-crate bugs.
4. **Product implication:** don't mandate rdbg. Availability alone yields ~0% use;
   a blanket mandate wastes tokens. The only justified path is a *selective*
   trigger that fires when a read loop is actually failing — and the bar to beat
   "just re-read the code" is high.

## Are debugger trajectories better *learning* material? (grounding analysis)

20 transcripts (10 read-loop, 10 debugger) scored by Opus teammates on grounded
observation vs confabulation. Means:

| | control (read) | strong (rdbg) |
|---|---|---|
| grounded runtime observations | 0.5 | **1.6** (3.2×) |
| grounding score (1–5) | 2.0 | **3.1** |
| unverified runtime claims | 2.0 | 1.8 (−10% only) |
| tool friction | 0.3 | **3.9** (13×) |
| wrong turns | 0.0 | 1.4 |
| aha came from | reading 10/10 | reading 8/10, rdbg 2/10 |

**What's real:** debugger trajectories carry ~3× more grounded runtime facts and
score higher on grounding. Read-loop trajectories are near-empty of observed
fact and actively **confabulate** — they assert unobserved I/O (`[7,9]→80`) as if
executed, ~2 fabricated claims per run. So as data that teaches *how code runs*,
the debugger set is genuinely richer, and the read set teaches a bad habit.

**What's not:** the debugger does NOT stop the model confabulating (claims only
2.0→1.8). The fix insight came from **reading in 80%** of debugger runs — rdbg was
mostly post-hoc verification (the true epistemic source in ~2–4/10). And ~13× of
the "extra tokens" is **tool friction** (rdbg CLI arg errors — `--lib`
unsupported, test-target naming), which is anti-signal that teaches flailing.

**Per training regime:**
- **SFT / distillation** — modest win *if curated*: keep the grounded,
  non-redundant runs; drop the friction/failed ones. Its real value is not
  teaching confabulation (which the read set does).
- **Outcome-RL (pass-only reward)** — net-negative. Reading solved 80%; the extra
  tokens + friction are pure cost; RL correctly learns to drop rdbg. (Matches the
  token-ROI result.)
- **Process / grounding-aware reward** — the regime where it *pays*, and the only
  one whose reward can even see the difference. Shape on "observe before assert,"
  but reward observations that DISAMBIGUATE (not re-confirm) and subtract friction,
  or the model farms debugger calls for grounding credit.

Side finding worth acting on: most friction is rdbg CLI ergonomics — fixing
`--lib`/test-target handling would clean up both the tool and the trajectories.
