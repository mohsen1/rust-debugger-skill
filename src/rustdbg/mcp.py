"""rustdbg MCP server -- exposes the debugger to MCP clients (Claude Code, Codex).

A minimal, dependency-free MCP stdio server (newline-delimited JSON-RPC 2.0). It
does not do any debugging itself: each tool call ensures the per-project rustdbg
daemon is running and forwards the request to it (the same daemon the ``rdbg``
CLI uses), then returns the result as text.

Run it as ``rustdbg-mcp`` (installed) or ``python -m rustdbg.mcp``. The project
is discovered from the working directory the client launches the server in
(its git root), so configure one server per project or launch it at the repo.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .cli import cargo_build, ensure_daemon, parse_bp, request, ws_root

PROTOCOL_VERSION = "2024-11-05"

# --------------------------------------------------------------------------- #
# Tool definitions: (name, description, input schema, handler)
# --------------------------------------------------------------------------- #

_S = lambda **props: {"type": "object", "properties": props}  # noqa: E731


def _bp_lines(ws, r):
    return r


def _launch(ws: Path, a: dict) -> dict:
    if a.get("cargo"):
        program = cargo_build(Path(a["cargo"]).resolve(), a.get("bin"), a.get("test"))
    elif a.get("bin_path"):
        program = Path(a["bin_path"]).resolve()
    else:
        return {"error": "provide either `cargo` (project dir) or `bin_path`"}
    base = Path(a.get("cwd") or Path.cwd())
    bps = [{"file": p, "line": ln}
           for p, ln in (parse_bp(b, base) for b in a.get("breakpoints", []))]
    return request(ws, {"cmd": "launch", "program": str(program), "cwd": str(program.parent),
                        "args": a.get("args", []), "breakpoints": bps,
                        "fn_breaks": a.get("fn_breaks", []), "panic": a.get("panic", False)})


TOOLS = [
    ("debug_launch",
     "Build (or take) a Rust debug binary and run it to the first breakpoint. "
     "Provide `cargo` (project dir, with optional `bin`/`test` target) or `bin_path`. "
     "`breakpoints` are 'file.rs:line' strings; `panic:true` also breaks where any "
     "panic is raised. Returns the stop location + source.",
     _S(cargo={"type": "string"}, bin={"type": "string"}, test={"type": "string"},
        bin_path={"type": "string"}, cwd={"type": "string"},
        breakpoints={"type": "array", "items": {"type": "string"}},
        fn_breaks={"type": "array", "items": {"type": "string"}},
        args={"type": "array", "items": {"type": "string"}},
        panic={"type": "boolean"}),
     _launch),
    ("debug_add_breakpoint",
     "Add a breakpoint while paused or before running. `file`+`line`, or `fn` for a "
     "function breakpoint, or `panic:true` for a Rust panic breakpoint, or `watch` "
     "to break when a local variable changes. Line breakpoints accept `condition` "
     "(simple expr), `hit` (Nth hit), `log` (logpoint message).",
     _S(file={"type": "string"}, line={"type": "integer"}, fn={"type": "string"},
        panic={"type": "boolean"}, watch={"type": "string"},
        condition={"type": "string"}, hit={"type": "integer"}, log={"type": "string"}),
     lambda ws, a: (
         request(ws, {"cmd": "bp_fn", "name": a["fn"]}) if a.get("fn") else
         request(ws, {"cmd": "bp_panic"}) if a.get("panic") else
         request(ws, {"cmd": "bp_watch", "var": a["watch"]}) if a.get("watch") else
         request(ws, {"cmd": "bp_add", "file": str(Path(a["file"]).resolve()
                                                    if Path(a["file"]).is_absolute()
                                                    else (Path.cwd() / a["file"]).resolve()),
                      "line": int(a["line"]), "condition": a.get("condition"),
                      "hit": a.get("hit"), "log": a.get("log")}))),
    ("debug_breakpoints", "List all breakpoints with their ids.",
     _S(), lambda ws, a: request(ws, {"cmd": "bp_list"})),
    ("debug_remove_breakpoint", "Remove a breakpoint by id (or 'panic').",
     _S(id={"type": "string"}), lambda ws, a: request(ws, {"cmd": "bp_rm", "id": a["id"]})),
    ("debug_continue", "Resume until the next breakpoint / stop.",
     _S(), lambda ws, a: request(ws, {"cmd": "continue"})),
    ("debug_step", "Step the current thread: over | in | out | insn (one instruction).",
     _S(kind={"type": "string", "enum": ["over", "in", "out", "insn"]}),
     lambda ws, a: request(ws, {"cmd": "step", "kind": a.get("kind", "over")})),
    ("debug_run_to", "Run to a specific line ('file.rs:line').",
     _S(location={"type": "string"}),
     lambda ws, a: request(ws, {"cmd": "until",
                                **dict(zip(("file", "line"), parse_bp(a["location"], Path.cwd())))})),
    ("debug_pause", "Interrupt a running program.",
     _S(), lambda ws, a: request(ws, {"cmd": "pause"})),
    ("debug_restart", "Relaunch with the same line/function/panic breakpoints.",
     _S(), lambda ws, a: request(ws, {"cmd": "restart"})),
    ("debug_locals", "Local variables at the current frame, with real Rust values.",
     _S(depth={"type": "integer"}),
     lambda ws, a: request(ws, {"cmd": "vars", "depth": a.get("depth", 3)})),
    ("debug_eval", "Evaluate a variable path (e.g. items[0].qty) at the current frame.",
     _S(path={"type": "string"}), lambda ws, a: request(ws, {"cmd": "eval", "expr": a["path"]})),
    ("debug_set", "Change a variable's value (e.g. path=cfg.threads value=8).",
     _S(path={"type": "string"}, value={"type": "string"}),
     lambda ws, a: request(ws, {"cmd": "set", "path": a["path"], "value": a["value"]})),
    ("debug_backtrace", "Backtrace of the current thread (trimmed to your code).",
     _S(), lambda ws, a: request(ws, {"cmd": "bt"})),
    ("debug_source", "Source lines around the current stop.",
     _S(radius={"type": "integer"}),
     lambda ws, a: request(ws, {"cmd": "list", "radius": a.get("radius", 6)})),
    ("debug_state", "The current stop, locals, and watch expressions in one call.",
     _S(), lambda ws, a: request(ws, {"cmd": "state"})),
    ("debug_threads", "List threads (all stop together).",
     _S(), lambda ws, a: request(ws, {"cmd": "threads"})),
    ("debug_select_thread", "Switch the current thread by id.",
     _S(id={"type": "integer"}), lambda ws, a: request(ws, {"cmd": "thread", "id": a["id"]})),
    ("debug_select_frame", "Select a stack frame by index (0 = innermost).",
     _S(index={"type": "integer"}),
     lambda ws, a: request(ws, {"cmd": "frame", "index": a["index"]})),
    ("debug_watch_expr", "Add or remove a watch expression (shown at every stop).",
     _S(action={"type": "string", "enum": ["add", "rm", "list"]}, expr={"type": "string"}),
     lambda ws, a: request(ws, {"cmd": "watch_expr", "action": a.get("action", "list"),
                                "expr": a.get("expr")})),
    ("debug_where", "Find a function/type/const across the workspace (rust-analyzer).",
     _S(query={"type": "string"}), lambda ws, a: request(ws, {"cmd": "where", "query": a["query"]})),
    ("debug_definition", "Go to definition at file:line:col (1-based) (rust-analyzer).",
     _S(file={"type": "string"}, line={"type": "integer"}, col={"type": "integer"}),
     lambda ws, a: request(ws, {"cmd": "def", "file": a["file"], "line": a["line"], "col": a["col"]})),
    ("debug_hover", "Type/signature/docs at file:line:col (rust-analyzer).",
     _S(file={"type": "string"}, line={"type": "integer"}, col={"type": "integer"}),
     lambda ws, a: request(ws, {"cmd": "hover", "file": a["file"], "line": a["line"], "col": a["col"]})),
    ("debug_references", "Find references at file:line:col (rust-analyzer).",
     _S(file={"type": "string"}, line={"type": "integer"}, col={"type": "integer"}),
     lambda ws, a: request(ws, {"cmd": "refs", "file": a["file"], "line": a["line"], "col": a["col"]})),
    ("debug_stop", "End the debug session (keeps the daemon warm).",
     _S(), lambda ws, a: request(ws, {"cmd": "stop"})),
]

_HANDLERS = {name: handler for name, _d, _s, handler in TOOLS}
_SCHEMAS = [{"name": n, "description": d, "inputSchema": {**s, "properties": s.get("properties", {})}}
            for n, d, s, _h in TOOLS]


def _format(resp: dict | None) -> tuple[str, bool]:
    """Turn a daemon response into (text, is_error) for MCP content."""
    if resp is None:
        return ("The rustdbg daemon did not respond (is a debug session running? "
                "call debug_launch first).", True)
    if not resp.get("ok", True):
        return (f"error: {resp.get('error', 'unknown')}", True)
    # pull the most informative field
    for key in ("stop", "vars", "value", "bt", "source", "threads", "breakpoints",
                "hover", "watches"):
        if key in resp and resp[key] not in (None, ""):
            v = resp[key]
            return (json.dumps(v, indent=2) if isinstance(v, (dict, list)) else str(v), False)
    if "symbols" in resp:
        return ("\n".join(f"{s['name']}  {s['file']}:{s['line']}" for s in resp["symbols"])
                or "(no matches)", False)
    if "locations" in resp:
        return ("\n".join(f"{l['file']}:{l['line']}:{l['col']}" for l in resp["locations"])
                or "(no results)", False)
    return (json.dumps({k: v for k, v in resp.items() if k != "ok"}) or "ok", False)


# --------------------------------------------------------------------------- #
# MCP stdio loop
# --------------------------------------------------------------------------- #


def _reply(id_, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> int:
    ws = ws_root()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}

        if method == "initialize":
            _reply(mid, {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "rustdbg", "version": "0.1.0"}})
        elif method in ("notifications/initialized", "initialized"):
            continue  # notification, no reply
        elif method == "ping":
            _reply(mid, {})
        elif method == "tools/list":
            _reply(mid, {"tools": _SCHEMAS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            handler = _HANDLERS.get(name)
            if handler is None:
                _reply(mid, error={"code": -32601, "message": f"unknown tool {name!r}"})
                continue
            try:
                ensure_daemon(ws)
                text, is_error = _format(handler(ws, args))
            except Exception as exc:  # noqa: BLE001
                text, is_error = f"{type(exc).__name__}: {exc}", True
            _reply(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})
        elif method == "shutdown":
            _reply(mid, {})
        elif mid is not None:
            _reply(mid, error={"code": -32601, "message": f"method {method!r} not found"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
