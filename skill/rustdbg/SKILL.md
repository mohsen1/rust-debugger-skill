---
name: rust-debugger
description: IDE-grade debugging for Rust — breakpoints (line/function/conditional/hit-count/panic/watchpoint), run + step over/in/out, read and CHANGE runtime values (Vec/String/struct/enum), multi-thread and stack-frame navigation, watch expressions, run-to-line, pause, plus rust-analyzer navigation (where/def/hover/refs). Use when a Rust program or test produces a wrong value or panics and you need to see/step through actual runtime state instead of adding println!/dbg!, to break exactly where a panic is raised, to watch a value change, or to find where a symbol is defined/used.
---
# rust-debugger (rdbg)

> Requires `pip install rustdbg` (provides the `rdbg` command) plus `rust-analyzer` and `lldb-dap`. See https://github.com/mohsen1/rustdbg


A full debugger for coding agents: set breakpoints, run, read the REAL runtime
values of variables, step, change values, and navigate — the editor debugging
experience from stateless CLI calls. Built on rust-analyzer + lldb-dap. Run
`rdbg` from anywhere inside the target project (debug build required).

## Start a session

```
rdbg where parse_config                # find where to break (rust-analyzer)
rdbg launch --cargo . --bin app --break src/config.rs:88 -- --threads 4
rdbg launch --cargo . --test mymod --break src/mymod.rs:120 -- my_failing_test
rdbg launch --bin-path target/debug/app --break src/main.rs:11   # skip the build
```
Add `--panic` to also break where any panic is raised, or `--break-fn <name>`.

## Breakpoints (add/change ANY time, even while paused)

```
rdbg break src/x.rs:42                  # line breakpoint
rdbg break src/x.rs:42 --if "i == 5"    # conditional (simple int/bool exprs)
rdbg break src/x.rs:42 --hit 3          # break on the 3rd hit
rdbg break src/x.rs:42 --log "i={i}"    # logpoint (print, don't stop)
rdbg break --fn my_crate::do_thing      # break entering a function
rdbg break --panic                      # break where a Rust panic is raised
rdbg watch cfg.threads                  # WATCHPOINT: break when a value changes
rdbg breaks                             # list (with ids); break-rm/on/off <id>
```

## Run control

```
rdbg continue                           # to the next stop
rdbg step over | in | out | insn        # step a source line, or one instruction
rdbg until src/x.rs:99                  # run to a line
rdbg pause                              # interrupt a running program
rdbg restart                            # relaunch with the same breakpoints
```

## Inspect and CHANGE state

```
rdbg vars                               # locals with real Rust values
rdbg eval items[0].qty                  # evaluate a variable path
rdbg set cfg.threads = 8                # change a variable mid-run
rdbg watch-expr add total               # re-shown at every stop
rdbg list                               # source around the current line
rdbg bt                                 # backtrace of the current thread
rdbg state                              # stop + locals + watches at once
```

## Threads and frames

```
rdbg threads                            # list threads (all stop together)
rdbg thread <id>                        # switch the current thread
rdbg frame <n> | up | down              # select a stack frame; vars/eval follow it
```

## Navigate (rust-analyzer)

```
rdbg where <Name> ; rdbg def|hover|refs <file> <line> <col>
```

`rdbg stop` ends the debug session; `rdbg down` stops the daemon.

## Typical loops

- **Wrong value:** break where it is computed, `vars`/`eval` to see the real
  inputs, `step` to watch it go wrong, `set` to test a fix hypothesis live.
- **Panic:** `launch ... --panic`, then `bt` and `up` to your frame to see the
  arguments that caused it.
- **Unexpected mutation:** `watch <var>` and `continue` to stop the instant it
  changes.
- **Failing test:** `--test <name> ... -- <test_filter>`, break in the assertion.

## Limits (Apple lldb-dap; documented, not worked around)

- `eval`/`set`/breakpoint-conditions take variable PATHS and simple primitive
  comparisons, NOT arbitrary Rust expressions (`a + b`, method calls). Put
  `codelldb` on PATH (auto-detected) to lift this.
- No set-next-statement / reverse debugging. Native restart is emulated by relaunch.
- Multi-thread: threads stop together; on macOS the worker-thread list at a
  breakpoint can be partial (adapter quirk) — the stopped thread is always usable.
- Build with debug info (default `cargo build`; a `--release` binary has little
  to inspect). One paused process per project; `rdbg down` (or 30-min idle)
  releases it.
