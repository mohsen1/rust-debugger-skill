---
name: rust-debugger
description: Debug a Rust program or failing test with rdbg — set breakpoints (line, function, conditional, hit-count, panic, or watchpoint), run and step, read locals as real Rust values (Vec/String/struct/enum), change a variable mid-run, and jump to definition/hover/references via rust-analyzer. Reach for it on a *runtime* question in large or complex code — a wrong computed value, an unexpected branch/type/state, or a panic — where reading and grep have stalled and you need to see actual runtime state instead of adding println!/dbg!. Read first for small, localized, or missing-output bugs a quick read already pins down; the debugger earns its cost mainly where the code is too large or the flow too tangled to trace by eye.
---

# rust-debugger

Debug from the command line with `rdbg`. It holds one paused process per project,
so breakpoints and state carry across calls. The target must be built with debug
info (the default `cargo build`). Run `rdbg` with no arguments for the full list.

Requires `rdbg` on `PATH` (`curl -fsSL https://azimi.me/rust-debugger-skill/install.sh | sh`),
plus `rust-analyzer` and `lldb-dap`.

## When to reach for it (and when not)

Read first. The debugger earns its cost only on a question you can't answer by
reading — and on a large repo every `launch` rebuilds, so a wasted debugging detour
is expensive. Decide *before* you launch:

**Reach for rdbg** when you have a **runtime question at a place you can name**:
- a value is **wrong** and you need the real inputs/flow that produced it;
- an **unexpected branch, type, or state** at runtime that reading can't pin down;
- a **panic** — `rdbg debug --panic` lands on the culprit frame in one call;
- you want to **test a fix live** with `set --then continue` before editing + rebuilding.
The biggest wins: wrong or extra output you can break at and trace *backward* to the
deciding code, in a codebase too large to follow by eye.

**Don't launch — just read/grep — when**:
- the failing test plus a quick read already point at the fix (small, localized bugs):
  debugging only adds fixed build + session overhead;
- the output is **missing** — nothing is emitted to break on, so finding the *absent*
  check is a reading task; the debugger can't trace code that never ran;
- you're **iterating on a candidate fix**: re-run the narrowed test, or validate the
  hypothesis with `set --then continue` — don't re-`launch` after every edit.

**Stay cheap.** Keep launches few: one session with several breakpoints, or one
`trace`, beats re-launching (each rebuilds). If 2–3 probes haven't localized it, stop
and go back to reading — you're probably at the wrong layer, and more debugging will
only burn tokens.

**Fix once, don't churn.** When you've found the cause, make **one** careful, minimal
edit and run the narrowed test. If it still fails, do **not** guess-and-edit again —
that's the most expensive way to work on a large repo (every build is costly, and each
wrong edit can make it worse). Instead read the exact failing assertion, or break at it
and `vars`/`eval` the actual-vs-expected values to see *why* before you touch the code
again. Better still, validate a fix hypothesis **without editing at all**: `set` the
suspect value and `continue` to watch the outcome change. More than ~2–3 edit→test
cycles means you're guessing — go back to understanding.

## Tap, don't walk (hard rules)

The debugger **aims your reading**; it rarely hands you the fix. The pattern that works: break
at the **sink** — where the wrong result surfaces (the emit, the return, the failing assert) —
read **which path fired and the deciding values there**, `bt` back to the code that decided it,
and **read that code**. One or two launches, then you're reading.

Treat the rest as hard rules, not advice — every losing run in the benchmark broke one of these
while sure it was "making progress":

- **`bt` named a `file:line` → STOP; do not launch again.** Read that line. The fix is in that
  frame or its **caller** — never break into a *callee* the backtrace names just to "confirm"
  its return value; read the caller that decided to use it.
- **Budget: 2 launches (a `trace` counts — it rebuilds the crate too).** Before a 3rd probe,
  state in one sentence the exact runtime fact you still lack *and cannot get by reading*. If
  you can't, you're done debugging — read.
- **`0 hits` / `NOT BOUND` means the sink is elsewhere: READ, don't re-guess.** Grep the wrong
  value / read the emit site to find the real `file:line`; do not relaunch at another guessed
  location. Never use the debugger to *search* for where to break — a failing test that names
  the wrong value (an error code, a message) is a grep task, not a debugger task.
- **`eval` can't run Rust methods.** A `.len()`, a formatted type name, any method-computed
  value will fail — break *inside* that method and read its inputs, or read the code. A failed
  `eval` on an opaque `Arc`/`Box`/trait object is **not** a "flaky debugger." **Never add
  `dbg!`/`eprintln!`/`println!`** — it rebuilds the crate and is the signature of every losing
  run. Use `rdbg vars` to see *all* locals at one stop instead of re-launching to change
  `--capture`.
- **State only what the output shows.** Cite the exact command and the output line that proves
  a claim, or say "unknown" — never assert an observation you can't quote. A test that just
  `FAILED` / `exited code 101` with **no `>>> STOP` and no hit count** ran to completion
  *without pausing* — it does **not** mean that line never ran; run `rdbg breaks` to confirm a
  breakpoint is **bound** before trusting its silence.
- **Degraded tooling → stop and read.** A DWARF/parse error, breakpoints that won't bind, or
  values that won't `eval` mean the debugger can't answer on this build; relaunching won't make
  it bind. Stepping instruction-by-instruction is walking execution — one short hop at most,
  never to traverse a path.

## Start a session

```
rdbg where parse_config                            # find where to break
rdbg launch --cargo . --bin app --break src/config.rs:88 -- --threads 4
rdbg launch --cargo . --lib --break src/lib.rs:42 -- my_test        # a #[test] in the library
rdbg launch --cargo . --test mytest --break tests/mytest.rs:12 -- some_case  # tests/mytest.rs
rdbg launch --bin-path target/debug/app --break src/main.rs:11   # skip the build
```

Pick the target by where the test lives: `--lib` for a `#[test]` inside the
library (`#[cfg(test)] mod tests` in `src/` — the common case), `--test <name>`
only for an integration test file `tests/<name>.rs`. In both, the words after
`--` are the test-name filter, so exactly the test you name runs.

Add `--panic` to also stop where any panic is raised, or `--break-fn <name>`.

To watch a value evolve without stepping, `trace` instead of `launch` — it runs
through every hit and returns a table in one call:

```
rdbg trace --cargo . --bin app --break src/x.rs:42 --capture i,sum --max 30
rdbg trace --cargo . --lib --break src/lib.rs:42 --capture a,b -- my_test
```

## Breakpoints

Set or change these any time, including while paused.

```
rdbg break src/x.rs:42                # line
rdbg break src/x.rs:42 --if "i == 5"  # conditional (simple comparisons)
rdbg break src/x.rs:42 --hit 3        # on the 3rd hit
rdbg break src/x.rs:42 --log "i={i}"  # logpoint (print, don't stop)
rdbg break --fn my_crate::do_thing    # entering a function
rdbg break --panic                    # where a Rust panic is raised
rdbg watch cfg.threads                # when a value changes
rdbg breaks                           # list with ids; break-rm/break-on/break-off <id>
```

## Run and step

```
rdbg continue
rdbg continue --until 'sum >= 100'    # keep resuming until a condition holds
rdbg step over | in | out | insn
rdbg until src/x.rs:99                 # run to a line
rdbg pause                            # interrupt a running program
rdbg restart
```

`continue --until '<path> <op> <value>'` (ops `== != < <= > >=`) re-checks the
condition at each breakpoint stop itself — one call instead of a
continue/eval loop per iteration, and it works where lldb conditional
breakpoints don't fire. Needs an active breakpoint to stop at; ends at the
first stop where the condition holds, or reports that the program exited.

## Read and change state

```
rdbg vars                             # locals with real Rust values
rdbg eval items[0].qty sum            # one or more variable paths (not method calls)
rdbg set cfg.threads = 8 --then continue   # change a value and resume
rdbg set cfg.threads = 8              # change a value
rdbg watch-expr add total             # re-shown at every stop
rdbg bt                               # backtrace
rdbg list                             # source around the current line
rdbg state                            # stop + locals + watches together
```

## Threads and frames

```
rdbg threads
rdbg thread <id>
rdbg frame <n> | up | down            # vars/eval follow the selected frame
```

## Navigate

```
rdbg where <Name>
rdbg def | hover | refs <file> <line> <col>
```

`rdbg stop` ends the session; `rdbg down` stops the daemon.

## Common loops

- **Wrong or extra output.** Break at the **sink** where it surfaces, `bt` to the
  deciding code and read it; `vars`/`eval` the real inputs there, `set` to test a fix
  without recompiling. Reach for `step` only for a short hop you can't read — not to
  walk the whole path.
- **Missing output.** Break at the sink anyway: if it shows `0 hits`, the emit never
  ran — read the upstream gate that returned early or accepted wrongly. Don't hunt.
- **Value goes wrong at some iteration.** Break in the loop, then
  `continue --until 'sum > 100'` to jump straight to the first stop where the
  condition holds instead of continue/eval-ing by hand.
- **Panic.** `rdbg debug --cargo . --lib --panic -- <test>` (or `--bin`/`--test`)
  runs to the panic and returns the message, the first *user* frame with its
  arguments and locals, and a backtrace in one call. (Or `launch … --panic`, then
  `bt`/`up` to your frame, if you want to keep poking around after.)
- **Unexpected mutation.** `watch <var>`, then `continue` to stop the moment it
  changes.
- **Failing test.** `--lib … -- <test_name>` for a `#[test]` in the library,
  `--test <name> … -- <test_name>` for `tests/<name>.rs`; break at the assertion
  or inside the code under test.

## Notes

- `eval`, `set`, and conditions take variable paths and simple comparisons, not
  arbitrary Rust expressions. `codelldb` on `PATH` lifts this.
- Debug the debug build; a `--release` binary has little to inspect.
- One paused process per project; `rdbg down` (or 30 minutes idle) releases it.
