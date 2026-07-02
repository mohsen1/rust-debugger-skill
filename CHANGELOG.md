# Changelog

## 0.1.0

Initial release.

- `rdbg` CLI and `rustdbg-mcp` MCP server (24 tools), sharing one per-project
  daemon that holds a paused `lldb-dap` session + a warm `rust-analyzer`.
- Breakpoints: line, function, conditional, hit-count, logpoint, panic,
  watchpoint (data breakpoint), with list/remove/enable/disable.
- Run control: continue, step over/in/out/instruction, run-to-line, pause,
  restart.
- Inspect & mutate: readable Rust locals, variable-path evaluate, set variable,
  watch expressions, backtrace, source listing.
- Navigation via rust-analyzer: where / definition / hover / references.
- Threads and stack-frame selection.
