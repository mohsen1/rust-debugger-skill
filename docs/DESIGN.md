# rust-debugger: IDE feature set (mapped to verified lldb-dap mechanics)

Every feature below is backed by a capability probe against Apple's lldb-dap on a
threaded/panicking Rust program (see the "verified" column). Features the adapter
does not support are listed at the end with fallbacks, so nothing is promised that
does not work.

## Breakpoints (client-side model, re-sent to the adapter on any change)

The session owns the full breakpoint list and stable ids; DAP's `setBreakpoints`
/ `setFunctionBreakpoints` / `setDataBreakpoints` each REPLACE all breakpoints of
their kind, so every add/remove/enable/disable re-sends the relevant full list.

| feature | rdbg command | DAP | verified |
|---|---|---|---|
| line breakpoint | `break <file:line>` | setBreakpoints | yes |
| function breakpoint | `break --fn <name>` | setFunctionBreakpoints | yes (`compute`, `crate::compute`) |
| conditional | `break <f:l> --if <expr>` | condition field | advertised; expr eval limited (see caveats) |
| hit count | `break <f:l> --hit <N>` | hitCondition | yes (adapter-side count) |
| logpoint | `break <f:l> --log <msg>` | logMessage | yes (static text; `{var}` interp is eval-limited) |
| **panic breakpoint** | `break --panic` | fn bp on `rust_panic` + `core::panicking::panic_fmt` | yes |
| **watchpoint (data)** | `watch <var>` | dataBreakpointInfo + setDataBreakpoints | yes |
| list / remove / enable / disable | `breaks`, `break rm <id>`, `break on/off <id>` | re-send | client-side |

## Threads (multi-thread debugging)

lldb-dap stops ALL threads on any stop (`allThreadsStopped=true`). The session
tracks the thread list and a current thread.

| feature | rdbg | DAP | verified |
|---|---|---|---|
| list threads | `threads` | threads | yes |
| select current thread | `thread <id>` | (client state) | yes |
| per-thread stack/vars | bt/vars use current thread | stackTrace{threadId} | yes |

## Frames (stack navigation)

Current frame index into the current thread's stack; vars/eval/list operate on it.

| feature | rdbg | DAP |
|---|---|---|
| select frame / up / down | `frame <n>` / `up` / `down` | scopes/variables/evaluate use that frameId |

## Run control

| feature | rdbg | DAP | verified |
|---|---|---|---|
| continue | `continue` | continue | yes |
| step over / in / out | `step over|in|out` | next / stepIn / stepOut | yes |
| step one instruction | `step insn` | next{granularity:instruction} | yes |
| run to line | `until <file:line>` | temp breakpoint + continue | yes |
| pause a running program | `pause` | pause | yes |
| restart | `restart` | disconnect + relaunch (no native restart) | fallback |

## Inspection / mutation

| feature | rdbg | DAP | verified |
|---|---|---|---|
| locals (readable Rust values) | `vars` | scopes+variables (+ rust formatters) | yes |
| evaluate a variable PATH | `eval <path>` | evaluate{context:hover} | yes (`items[0].qty`) |
| **set a variable** | `set <path> = <value>` | setVariable (walk path to parent ref) | yes (acc -> 777) |
| watch expressions | `watch-expr <path>` shown each stop | client-side re-eval | yes |
| source at current line | `list` | read the frame's source file | yes |
| backtrace | `bt` | stackTrace | yes |

## Static navigation (rust-analyzer) -- unchanged

`where` / `def` / `hover` / `refs`.

## Not supported by Apple lldb-dap (documented, not promised)

- Arbitrary-expression eval (`a + b`, method calls): variable PATHS only. Condition
  and logpoint-interpolation expressions inherit this -- keep conditions to simple
  primitive comparisons; a complex condition that fails to evaluate makes the
  breakpoint fire unconditionally (safe, but not filtered).
- Set-next-statement / goto (`supportsGotoTargetsRequest` = false).
- Reverse / step-back debugging (`supportsStepBack` = false).
- Native restart (`supportsRestartRequest` = false) -> emulated by relaunch.
- No Rust exception filter -> panic handled via the function-breakpoint route above.
- `codelldb` on PATH (auto-detected) lifts the expression-eval limits.
