# Benchmarks

Do coding agents fix bugs faster / cheaper when they can reach for `rdbg`?

Each task is a small Rust crate with a planted bug and a failing test. The
harness runs an agent (`claude` or `codex`) headless on the same prompt, once
**without** rdbg and once **with** it (the skill installed and a one-line
pointer), then records wall time, token usage, and whether `cargo test` passes.

```sh
python3 bench.py                                   # all tasks, both agents
python3 bench.py --agents claude --tasks accumulator --repeat 3
```

Runs make real API calls and cost money. The `with` condition needs `rdbg` on
PATH (`curl -fsSL https://azimi.me/rust-debugger-skill/install.sh | sh`) plus
`rust-analyzer` and `lldb-dap`.

## Tasks

- `accumulator` — a data pipeline returns the wrong number (a filter keeps the
  wrong elements). The failure doesn't point at the line; inspecting the
  intermediate value localizes it.
- `panic_index` — an off-by-one index panics on valid input. A panic breakpoint
  lands on the frame with the bad index.

Add a task by dropping a crate under `tasks/<name>/` with a failing test and a
`PROMPT.md`.

## Output

`results/runs.json` plus a printed table of per-run and with-vs-without means
(pass rate, wall seconds, tokens, cost).

## Reading the results

Token cost is the primary signal — every extra rebuild-and-print cycle is a
model turn. Wall time is secondary (it includes build time either way). Expect
the benefit to grow with how hard the bug is to spot by reading.
