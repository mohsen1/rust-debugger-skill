"""Rust-aware, stateful debug session on top of :mod:`dap` (lldb-dap).

Implements an IDE-grade feature set on ONE launched, paused debuggee: rich
breakpoints (line / function / conditional / hit-count / log / panic / data
watchpoints), multi-thread control, stack-frame navigation, readable Rust
locals, variable-path evaluation, variable mutation, watch expressions, run-to-
line, pause, and (emulated) restart. Every mechanic here was verified against the
adapter (see ../DESIGN.md).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .dap import DapClient, DapError

# --------------------------------------------------------------------------- #
# Adapter + rust formatter discovery
# --------------------------------------------------------------------------- #


def find_lldb_dap() -> str | None:
    for name in ("lldb-dap", "lldb-vscode"):
        p = shutil.which(name)
        if p:
            return p
    try:
        proc = subprocess.run(["xcrun", "-f", "lldb-dap"],
                              capture_output=True, text=True, timeout=15)
        cand = proc.stdout.strip()
        if proc.returncode == 0 and cand and Path(cand).exists():
            return cand
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def rust_formatter_commands() -> list[str]:
    try:
        sysroot = subprocess.run(["rustc", "--print", "sysroot"],
                                 capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    etc = Path(sysroot) / "lib/rustlib/etc"
    lookup, commands = etc / "lldb_lookup.py", etc / "lldb_commands"
    if not (lookup.exists() and commands.exists()):
        return []
    return [f"command script import {lookup}", f"command source -s 0 {commands}"]


# Rust panic entry points -- a function breakpoint on these breaks WHERE a panic
# is raised (Apple lldb-dap has no Rust exception filter).
PANIC_SYMBOLS = ("rust_panic", "core::panicking::panic_fmt", "core::panicking::panic")

# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #

_MOD_SEG = re.compile(r"\b[a-z_][a-z0-9_]*::")
_LEAF_VALUE = re.compile(r'^(".*"|\'.*\'|-?\d|true$|false$|0x[0-9a-fA-F]+$|None$|\()')
_LEAF_TYPE = re.compile(r"(String$|str$|&str$|char$|bool$|^u\d|^i\d|^f\d|^usize|^isize)")
_MANGLE = {"$LT$": "<", "$GT$": ">", "$u20$": " ", "$C$": ",", "$LP$": "(", "$RP$": ")",
           "$u7b$": "{", "$u7d$": "}", "$RF$": "&", "$BP$": "*", "$u5b$": "[", "$u5d$": "]"}


def short_type(t: str) -> str:
    if not t:
        return ""
    t = _MOD_SEG.sub("", t).replace(", Global>", ">").replace("Global", "").strip()
    return t[:80]


def short_fn(name: str) -> str:
    name = re.sub(r"::h[0-9a-f]{8,}$", "", name)
    for k, v in _MANGLE.items():
        name = name.replace(k, v)
    return name.replace("..", "::")


@dataclass
class Frame:
    id: int
    name: str
    file: str
    line: int
    path: str = ""


@dataclass
class Stop:
    reason: str
    thread_id: int
    frames: list[Frame] = field(default_factory=list)
    exited: bool = False
    exit_code: int | None = None
    description: str = ""

    @property
    def top(self) -> Frame | None:
        return self.frames[0] if self.frames else None


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


class DebugSession:
    def __init__(self, program: Path | str, cwd: Path | str | None = None,
                 args: list[str] | None = None, adapter: str | None = None) -> None:
        self.program = str(Path(program).resolve())
        self.cwd = str(Path(cwd).resolve()) if cwd else str(Path(self.program).parent)
        self.args = args or []
        self.adapter = adapter or find_lldb_dap()
        if not self.adapter:
            raise DapError("no lldb-dap adapter (install LLVM or Xcode command line tools)")
        self.dap = DapClient([self.adapter])
        # breakpoint model (owned here; re-sent to the adapter on change)
        self._bp_id = 0
        self.line_bps: dict[str, list[dict]] = {}   # abspath -> [{id,line,condition,hit,log,enabled}]
        self.fn_bps: list[dict] = []                # [{id,name,enabled}]
        self.data_bps: list[dict] = []              # [{id,name,dataId,enabled}]
        self.panic = False
        self._temp: dict[str, set[int]] = {}        # run-to-line temporary lines
        # thread / frame state
        self.threads: list[dict] = []
        self.cur_thread: int | None = None
        self.cur_frame: int = 0
        self.watches: list[str] = []
        self.output: list[str] = []
        self.last_stop: Stop | None = None
        self._configured = False

    # -- launch --------------------------------------------------------------- #

    def launch(self, stop_on_entry: bool = False, timeout: float = 90.0) -> None:
        self.dap.request("initialize", {
            "adapterID": "lldb-dap", "clientID": "rustdbg", "linesStartAt1": True,
            "columnsStartAt1": True, "pathFormat": "path", "supportsVariableType": True,
            "supportsRunInTerminalRequest": False,
        }, timeout=timeout)
        seq = self.dap.send("launch", {
            "program": self.program, "cwd": self.cwd, "args": self.args,
            "stopOnEntry": stop_on_entry, "initCommands": rust_formatter_commands(),
        })
        self.dap.wait_event("initialized", timeout=timeout)
        for path in self.line_bps:
            self._sync_line(path)
        self._sync_fn()
        self.dap.request("configurationDone")
        self._configured = True
        self.dap.reply(seq, timeout=timeout)

    def restart(self, timeout: float = 90.0) -> "Stop":
        """No native restart on lldb-dap -> relaunch, keeping line/fn/panic
        breakpoints. Watchpoints are NOT carried over: their dataId is bound to
        the old session and a variable may not even be in scope at the new first
        stop; re-add them with `watch` once stopped."""
        try:
            self.dap.request_soft("disconnect", {"terminateDebuggee": True}, timeout=5)
        except DapError:
            pass
        self.dap.close()
        self.dap = DapClient([self.adapter])
        self._configured = False
        self.data_bps = []
        self.threads, self.cur_thread, self.cur_frame = [], None, 0
        self.launch(timeout=timeout)
        return self.run(timeout=timeout)

    # -- breakpoint model ----------------------------------------------------- #

    def _next_id(self) -> int:
        self._bp_id += 1
        return self._bp_id

    def _sync_line(self, path: str) -> list[dict]:
        active = [b for b in self.line_bps.get(path, []) if b["enabled"]]
        wire = [{"line": b["line"], **({"condition": b["condition"]} if b.get("condition") else {}),
                 **({"hitCondition": str(b["hit"])} if b.get("hit") else {}),
                 **({"logMessage": b["log"]} if b.get("log") else {})} for b in active]
        wire += [{"line": ln} for ln in sorted(self._temp.get(path, set()))]
        resp = self.dap.request("setBreakpoints",
                                {"source": {"path": path}, "breakpoints": wire})
        return resp["body"]["breakpoints"]

    def _sync_fn(self) -> None:
        names = [b["name"] for b in self.fn_bps if b["enabled"]]
        if self.panic:
            names += list(PANIC_SYMBOLS)
        self.dap.request_soft("setFunctionBreakpoints",
                              {"breakpoints": [{"name": n} for n in names]})

    def _sync_data(self) -> None:
        ids = [b["dataId"] for b in self.data_bps if b["enabled"] and b.get("dataId")]
        if ids or self.data_bps:
            self.dap.request_soft("setDataBreakpoints",
                                  {"breakpoints": [{"dataId": i} for i in ids]})

    def add_line_bp(self, path: str, line: int, condition: str | None = None,
                    hit: int | None = None, log: str | None = None) -> dict:
        abspath = str(Path(path).resolve())
        bp = {"id": self._next_id(), "kind": "line", "file": abspath, "line": line,
              "condition": condition, "hit": hit, "log": log, "enabled": True}
        self.line_bps.setdefault(abspath, []).append(bp)
        if self._configured:
            verified = self._sync_line(abspath)
            # match THIS breakpoint's own verified flag by its wire index (the
            # response array is 1:1 with the active list we sent, temps last).
            active = [b for b in self.line_bps[abspath] if b["enabled"]]
            idx = active.index(bp)
            bp["verified"] = bool(verified[idx].get("verified")) if idx < len(verified) else True
        return bp

    def add_fn_bp(self, name: str) -> dict:
        bp = {"id": self._next_id(), "kind": "fn", "name": name, "enabled": True}
        self.fn_bps.append(bp)
        if self._configured:
            self._sync_fn()
        return bp

    def add_panic_bp(self) -> dict:
        self.panic = True
        if self._configured:
            self._sync_fn()
        return {"id": "panic", "kind": "panic", "symbols": PANIC_SYMBOLS}

    def add_watchpoint(self, var: str) -> dict:
        """Data breakpoint: break when ``var`` (a local at the current frame) changes."""
        ref, name = self._resolve_var_ref(var)
        if ref is None:
            raise DapError(f"variable {var!r} not found in the current frame")
        info = self.dap.request_soft("dataBreakpointInfo",
                                     {"variablesReference": ref, "name": name})
        data_id = (info.get("body") or {}).get("dataId")
        if not data_id:
            raise DapError(f"cannot watch {var!r}: {(info.get('body') or {}).get('description', 'unsupported')}")
        bp = {"id": self._next_id(), "kind": "watch", "name": var,
              "dataId": data_id, "enabled": True}
        self.data_bps.append(bp)
        self._sync_data()
        return bp

    def all_bps(self) -> list[dict]:
        out: list[dict] = []
        for lst in self.line_bps.values():
            out += lst
        out += self.fn_bps + self.data_bps
        if self.panic:
            out.append({"id": "panic", "kind": "panic", "enabled": True,
                        "name": "rust panic"})
        return sorted(out, key=lambda b: (str(b["id"])))

    def remove_bp(self, bp_id: int | str) -> bool:
        if bp_id == "panic":
            self.panic = False
            self._sync_fn()
            return True
        for path, lst in self.line_bps.items():
            n = len(lst)
            self.line_bps[path] = [b for b in lst if b["id"] != bp_id]
            if len(self.line_bps[path]) != n:
                self._sync_line(path)
                return True
        for coll, sync in ((self.fn_bps, self._sync_fn), (self.data_bps, self._sync_data)):
            n = len(coll)
            coll[:] = [b for b in coll if b["id"] != bp_id]
            if len(coll) != n:
                sync()
                return True
        return False

    def set_enabled(self, bp_id: int | str, enabled: bool) -> bool:
        if bp_id == "panic":
            self.panic = enabled
            self._sync_fn()
            return True
        for path, lst in self.line_bps.items():
            for b in lst:
                if b["id"] == bp_id:
                    b["enabled"] = enabled
                    self._sync_line(path)
                    return True
        for coll, sync in ((self.fn_bps, self._sync_fn), (self.data_bps, self._sync_data)):
            for b in coll:
                if b["id"] == bp_id:
                    b["enabled"] = enabled
                    sync()
                    return True
        return False

    # -- run control ---------------------------------------------------------- #

    def _flush(self) -> None:
        """Discard events left over from the current all-threads-stopped state
        (lldb-dap emits one `stopped` per thread when many stop together); keep
        program output. Called before a resume so the next wait sees only the
        NEW stop, not a stale one."""
        while (ev := self.dap.poll_event(timeout=0.0)) is not None:
            if ev.get("event") == "output":
                b = ev["body"]
                if b.get("category") in (None, "stdout", "stderr", "console"):
                    self.output.append(b.get("output", ""))

    def _await_stop(self, timeout: float) -> Stop:
        import time as _t
        end = _t.monotonic() + timeout
        while _t.monotonic() < end:
            ev = self.dap.poll_event(timeout=max(0.05, end - _t.monotonic()))
            if ev is None:
                continue
            name = ev.get("event")
            if name == "output":
                b = ev["body"]
                if b.get("category") in (None, "stdout", "stderr", "console"):
                    self.output.append(b.get("output", ""))
            elif name == "exited":
                self.last_stop = Stop("exited", self.cur_thread or 0, exited=True,
                                      exit_code=ev["body"].get("exitCode"))
                return self.last_stop
            elif name == "terminated":
                self.last_stop = Stop("terminated", self.cur_thread or 0, exited=True)
                return self.last_stop
            elif name == "stopped":
                b = ev["body"]
                self.cur_thread = b.get("threadId") or self.cur_thread
                self.cur_frame = 0
                self._refresh_threads()
                self.last_stop = self._build_stop(b.get("reason", "stopped"),
                                                  b.get("description", ""))
                return self.last_stop
        raise DapError("no stop/exit event (program may still be running -- try `pause`)")

    def _refresh_threads(self) -> None:
        try:
            self.threads = self.dap.request("threads")["body"]["threads"]
        except DapError:
            self.threads = []

    def _frames(self, thread_id: int) -> list[Frame]:
        try:
            body = self.dap.request("stackTrace", {"threadId": thread_id, "levels": 40})["body"]
        except DapError:
            return []
        out = []
        for f in body.get("stackFrames", []):
            src = f.get("source") or {}
            out.append(Frame(id=f["id"], name=short_fn(f.get("name", "")),
                             file=src.get("name", "?"), line=f.get("line", 0),
                             path=src.get("path", "")))
        return out

    def _build_stop(self, reason: str, description: str) -> Stop:
        tid = self.cur_thread or 0
        return Stop(reason, tid, self._frames(tid), description=description)

    def run(self, timeout: float = 120.0) -> Stop:
        return self._await_stop(timeout)

    def _resume(self, command: str, args: dict | None, timeout: float) -> Stop:
        if self.cur_thread is None:
            raise DapError("not stopped")
        self._flush()  # drop stale per-thread stop events from the current stop
        self.dap.request(command, {"threadId": self.cur_thread, **(args or {})})
        return self._await_stop(timeout)

    def cont(self, timeout: float = 120.0) -> Stop:
        return self._resume("continue", None, timeout)

    def step_over(self, insn: bool = False, timeout: float = 120.0) -> Stop:
        return self._resume("next", {"granularity": "instruction"} if insn else None, timeout)

    def step_in(self, timeout: float = 120.0) -> Stop:
        return self._resume("stepIn", None, timeout)

    def step_out(self, timeout: float = 120.0) -> Stop:
        return self._resume("stepOut", None, timeout)

    def until(self, path: str, line: int, timeout: float = 120.0) -> Stop:
        """Run to a line (temporary breakpoint), then clean it up."""
        abspath = str(Path(path).resolve())
        self._temp.setdefault(abspath, set()).add(line)
        self._sync_line(abspath)
        try:
            return self.cont(timeout)
        finally:
            self._temp.get(abspath, set()).discard(line)
            self._sync_line(abspath)

    def pause(self, timeout: float = 30.0) -> Stop:
        self._flush()
        tid = self.cur_thread or (self.threads[0]["id"] if self.threads else 1)
        self.dap.request_soft("pause", {"threadId": tid})
        return self._await_stop(timeout)

    # -- threads / frames ----------------------------------------------------- #

    def select_thread(self, thread_id: int) -> bool:
        if any(t["id"] == thread_id for t in self.threads):
            self.cur_thread = thread_id
            self.cur_frame = 0
            self.last_stop = self._build_stop("switch", "")
            return True
        return False

    def threads_text(self) -> str:
        out = []
        for t in self.threads:
            fr = self._frames(t["id"])
            where = f"{fr[0].name} {fr[0].file}:{fr[0].line}" if fr else "?"
            mark = "*" if t["id"] == self.cur_thread else " "
            out.append(f" {mark} thread {t['id']} [{t.get('name','')}]  {where}")
        return "\n".join(out) or "(no threads)"

    def select_frame(self, index: int) -> bool:
        if self.last_stop and 0 <= index < len(self.last_stop.frames):
            self.cur_frame = index
            return True
        return False

    def frame_shift(self, delta: int) -> bool:
        return self.select_frame(self.cur_frame + delta)

    def _frame(self) -> Frame:
        if self.last_stop and self.last_stop.frames:
            i = min(self.cur_frame, len(self.last_stop.frames) - 1)
            return self.last_stop.frames[i]
        raise DapError("not stopped")

    # -- inspection / mutation ------------------------------------------------ #

    def _locals_ref(self) -> int:
        scopes = self.dap.request("scopes", {"frameId": self._frame().id})["body"]["scopes"]
        return next((s["variablesReference"] for s in scopes
                     if s.get("name", "").lower().startswith("local")), 0)

    def _resolve_var_ref(self, path: str) -> tuple[int | None, str]:
        """Walk a variable path to the parent variablesReference + leaf name.

        Tokenizes both field access and indexing: ``items[0].qty`` ->
        ``['items', '[0]', 'qty']`` (Vec/array children are named ``[0]`` in the
        DAP variables tree), so set/watch work on indexed paths, not just fields.
        """
        parts = re.findall(r"\[\d+\]|[^.\[\]]+", path)
        if not parts:
            return None, path
        ref = self._locals_ref()
        leaf = parts[-1]
        for seg in parts[:-1]:
            variables = self.dap.request("variables", {"variablesReference": ref})["body"]["variables"]
            match = next((v for v in variables if v["name"] == seg), None)
            if not match or not match.get("variablesReference"):
                return None, leaf
            ref = match["variablesReference"]
        return ref, leaf

    def locals_text(self, depth: int = 3, cap: int = 12) -> str:
        out: list[str] = []
        self._render(self._locals_ref(), depth, cap, "  ", out)
        return "\n".join(out) if out else "  (no locals)"

    def _render(self, ref: int, depth: int, cap: int, indent: str, out: list[str]) -> None:
        if not ref or depth <= 0:
            return
        try:
            variables = self.dap.request("variables", {"variablesReference": ref})["body"]["variables"]
        except DapError:
            return
        for v in variables[:cap]:
            name, val = v.get("name", "?"), v.get("value", "")
            typ = short_type(v.get("type", ""))
            child = v.get("variablesReference", 0)
            leaf = _LEAF_VALUE.match(val) or _LEAF_TYPE.search(typ or "") or not child
            shown = val if (leaf or _LEAF_VALUE.match(val)) else ""
            out.append(f"{indent}{name}: {typ}" + (f" = {shown}" if shown else ""))
            if not leaf and depth > 1:
                self._render(child, depth - 1, cap, indent + "  ", out)
        if len(variables) > cap:
            out.append(f"{indent}... ({len(variables) - cap} more)")

    def evaluate(self, expression: str) -> str:
        resp = self.dap.request_soft(
            "evaluate", {"expression": expression, "frameId": self._frame().id,
                         "context": "hover"})
        if not resp.get("success"):
            return f"(cannot evaluate {expression!r}: {resp.get('message', 'error')})"
        b = resp.get("body", {})
        return f"{short_type(b.get('type', ''))} = {b.get('result', '')}".strip(" =")

    def set_variable(self, path: str, value: str) -> str:
        ref, name = self._resolve_var_ref(path)
        if ref is None:
            return f"(variable {path!r} not found)"
        resp = self.dap.request_soft(
            "setVariable", {"variablesReference": ref, "name": name, "value": value})
        if not resp.get("success"):
            return f"(cannot set {path!r}: {resp.get('message', 'error')})"
        return f"{path} = {resp['body'].get('value')}"

    def watches_text(self) -> str:
        if not self.watches:
            return "  (no watch expressions)"
        return "\n".join(f"  {e}: {self.evaluate(e)}" for e in self.watches)

    def backtrace_text(self, limit: int = 20) -> str:
        if not self.last_stop:
            return "(not stopped)"
        out = []
        for i, f in enumerate(self.last_stop.frames[:limit]):
            mark = ">" if i == self.cur_frame else " "
            out.append(f" {mark}#{i} {f.name}  {f.file}:{f.line}")
            if f.name.endswith("::main") or f.name == "main":
                break
        return "\n".join(out)

    def source_around(self, radius: int = 6) -> str:
        f = self._frame()
        if not f.path or not Path(f.path).exists():
            return f"  ({f.file}:{f.line} -- source not available)"
        try:
            lines = Path(f.path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return f"  ({f.file}:{f.line})"
        lo, hi = max(0, f.line - radius - 1), min(len(lines), f.line + radius)
        out = [f"  {f.file}:{f.line}  (frame #{self.cur_frame} {f.name})"]
        for i in range(lo, hi):
            mark = "->" if i + 1 == f.line else "  "
            out.append(f"  {mark} {i + 1:>5} | {lines[i]}")
        return "\n".join(out)

    def disconnect(self) -> None:
        try:
            self.dap.request_soft("disconnect", {"terminateDebuggee": True}, timeout=5)
        except DapError:
            pass
        self.dap.close()
