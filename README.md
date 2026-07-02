# rustdbg

**IDE-grade Rust debugging for coding agents.** Give Claude Code, Codex, or any
MCP client the ability to set breakpoints, run, and read (and change) the *real
runtime values* of variables — instead of guessing from `println!`/`dbg!` and
recompiling.

Built on the tools you already have — [`rust-analyzer`](https://rust-analyzer.github.io/)
and `lldb-dap` — with **zero Python dependencies**. Ships as both a **skill**
(the `rdbg` CLI) and an **MCP server**.

```
$ rdbg launch --cargo . --bin app --break src/config.rs:88 -- --threads 4
>>> STOP [breakpoint] app::parse_config  config.rs:88  (thread 1)
    ->    88 |     let cfg = Config::from(&raw);
$ rdbg vars
  cfg: Config
    threads: usize = 4
    paths: Vec<PathBuf> (size=2) = [...]
$ rdbg eval cfg.threads          # usize = 4   -- the actual runtime value
$ rdbg set cfg.threads = 8       # change it live
$ rdbg step over ; rdbg continue
```

## Why

Static analysis (rust-analyzer) tells an agent what a symbol *is*. rustdbg adds
the missing half: what it *holds at runtime*. Break where a value is wrong, read
the actual `Vec`/`String`/struct/enum, step to watch it change, or break exactly
where a panic is raised — the editor debugging loop, driven from an agent's
stateless tool calls.

## Features

- **Breakpoints:** line, function, conditional (`--if`), hit-count (`--hit`),
  logpoint (`--log`), **panic** (break where a Rust panic is raised), and
  **watchpoints** (break when a value changes).
- **Run control:** continue, step over / in / out / instruction, run-to-line,
  pause a running program, restart.
- **Inspect & mutate:** locals with readable Rust values, evaluate variable
  paths (`items[0].qty`), **set a variable's value live**, watch expressions.
- **Navigate (rust-analyzer):** where / definition / hover / references.
- **Threads & frames:** list/switch threads, select stack frames.

See [`docs/DESIGN.md`](docs/DESIGN.md) for exactly what the debug adapter does and
does not support, verified against the real adapter.

## Requirements

- Python 3.9+ (standard library only)
- `rust-analyzer` — `rustup component add rust-analyzer`
- A DAP debug adapter: **`lldb-dap`** (ships with the Xcode command line tools on
  macOS, or LLVM on Linux: `apt install lldb` / `brew install llvm`). Optional:
  put [`codelldb`](https://github.com/vadimcn/codelldb) on `PATH` (auto-detected)
  for full Rust expression evaluation.
- A Rust project built with debug info (the default `cargo build`).

## Install

Straight from the repo (works today):

```bash
pip install "git+https://github.com/mohsen1/rustdbg"     # provides rdbg + rustdbg-mcp
```

Once published to PyPI:

```bash
pip install rustdbg
```

Or run from a clone with no install: `PYTHONPATH=src python3 -m rustdbg.cli --help`.

## Use it as an MCP server

Exposes ~24 tools (`debug_launch`, `debug_add_breakpoint`, `debug_step`,
`debug_locals`, `debug_eval`, `debug_set`, `debug_where`, ...).

**Claude Code** — add to `.mcp.json` in your project (or run
`claude mcp add rustdbg -- rustdbg-mcp`):

```json
{
  "mcpServers": {
    "rustdbg": { "command": "rustdbg-mcp" }
  }
}
```

**Codex** — add to `~/.codex/config.toml`:

```toml
[mcp_servers.rustdbg]
command = "rustdbg-mcp"
```

The server discovers your project from the directory it is launched in (its git
root) and manages the debug session per project.

## Use it as a skill (CLI)

`rdbg` is a small, stateful CLI; a companion skill for Claude Code / Codex lives
in [`skill/rustdbg/`](skill/rustdbg/SKILL.md). Drop it into `.claude/skills/` (or
`.agents/skills/`) after `pip install rustdbg`, and the agent will use `rdbg`
directly. Full command reference: `rdbg` with no args, or the SKILL.

## How it works

A per-project background daemon holds one paused debuggee (an `lldb-dap` session
with the Rust value formatters loaded) plus a warm `rust-analyzer` for
navigation, and serves commands over a Unix socket. The CLI and the MCP server
are both thin clients of that daemon — so a breakpoint set in one call is still
there in the next, and the process stays paused between an agent's tool calls.
The daemon auto-stops after 30 minutes idle. State lives in `.rustdbg/`
(git-ignore it).

## Limitations (honest, and mostly the adapter's)

- Apple's `lldb-dap` evaluates variable **paths** and simple primitive
  comparisons, not arbitrary Rust expressions (`a + b`, method calls). `codelldb`
  on `PATH` lifts this.
- No set-next-statement / reverse debugging; native restart is emulated by
  relaunch (watchpoints are re-added, not carried over).
- On macOS the worker-thread *list* at a breakpoint can be partial; the stopped
  thread is always usable.

## License

Dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your
option.
