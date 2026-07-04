# Benchmarks

Does a coding agent fix bugs faster and cheaper when it can reach for `rdbg`?

## tsz fix-rate benchmark (`bench_tsz.py`)

Real merged bug-fixes in `tsz` (a ~1.7M-line Rust type-checker), run twice per bug —
once plain, once with `rdbg` — measuring **fix rate and tokens**. Cases are chosen
post-training-cutoff and each is a clean single-commit checkout at the fix's parent with
only the regression test overlaid, so the agent cannot look the answer up (no future
history, web tools disallowed). See [`results-tsz/README.md`](results-tsz/README.md) for
the method and full results, and [`run3-full-sweep.md`](results-tsz/run3-full-sweep.md)
for the current run.

Headline (run 3, 22 cases, Opus): **−47% aggregate tokens, 100% fix rate, no systematic
waste.** A SKILL triage ("read first; debug only a runtime question in large code; skip
cheap/missing-output bugs") makes the agent reach for the debugger only when it pays —
big wins where the bug is expensive to read (−82% to −85%), ~neutral on cheap bugs. The
apparent +100% single-run cells are *variance*: multi-trialed, their medians are wins
(−5%, −39%). codelldb runs at 636MB on tsz (was ~20GB) via lazy symbol loading.

Runs make real API calls and cost money. The `with` condition needs `rdbg` on PATH
(`curl -fsSL https://azimi.me/rust-debugger-skill/install.sh | sh`) plus `rust-analyzer`
and a debug adapter (`install.sh` sets up codelldb).

```sh
# mine cases -> results-tsz/cases-tsz.json (see bench_tsz.py header), then per slot + capped image:
python3 bench_tsz.py --slot A --image /Volumes/tszA --cases 0,1,2,3 --model opus   # opus | sonnet
```
