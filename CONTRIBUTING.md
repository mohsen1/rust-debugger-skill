# Contributing

Thanks for your interest! rustdbg is a small, dependency-free Python package.

- **Run it locally without installing:** `PYTHONPATH=src python3 -m rustdbg.cli --help`
- **Package layout:** `src/rustdbg/` — `dap.py` (DAP transport), `session.py`
  (the debug session + breakpoint model), `lsp.py` (rust-analyzer navigation),
  `daemon.py` (per-project server), `cli.py` (the `rdbg` CLI), `mcp.py` (the MCP
  server). All standard library.
- **Capabilities are verified, not assumed.** If you add a feature that depends
  on the debug adapter, probe the real adapter first and record what works in
  `docs/DESIGN.md`.
- **Style:** keep it stdlib-only and small; match the surrounding code.

Please open an issue to discuss larger changes before a PR.
