"""rustdbg daemon: holds a paused debug session + a warm rust-analyzer nav
client for one project, and serves IDE commands over a Unix socket.

Debugging is stateful (a breakpoint pauses the process; you then inspect/step);
agent tool-calls are stateless. The daemon bridges the two: one long-lived
paused process, many short command invocations from the `rdbg` CLI.

State dir: .rustdbg/ under the project (socket, daemon.json, log).
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from .lsp import LspNav
from .session import DebugSession, find_lldb_dap

STATE = ".rustdbg"
IDLE_SHUTDOWN_S = 30 * 60


def _log(ws: Path, msg: str) -> None:
    try:
        (ws / STATE).mkdir(exist_ok=True)
        with open(ws / STATE / "daemon.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


class Daemon:
    def __init__(self, ws: Path) -> None:
        self.ws = ws.resolve()
        self.state = self.ws / STATE
        self.state.mkdir(exist_ok=True)
        self.session: DebugSession | None = None
        self.lsp: LspNav | None = None
        self._lsp_error = ""
        self.sock: socket.socket | None = None
        self._stop = threading.Event()
        self._last = time.monotonic()

    # -- lifecycle ------------------------------------------------------------- #

    def start(self) -> None:
        # Socket goes in a SHORT tmp path (AF_UNIX sun_path is ~104 bytes on
        # macOS; a project under a deep path would overflow ``<proj>/.rustdbg``).
        digest = hashlib.sha256(str(self.ws).encode()).hexdigest()[:16]
        base = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
        path = Path(base) / f"rustdbg-{digest}.sock"
        if path.exists():
            path.unlink()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(4)
        self._sock_path = path
        (self.state / "daemon.json").write_text(json.dumps(
            {"pid": os.getpid(), "socket": str(path)}))
        (self.state / "starting.pid").unlink(missing_ok=True)
        _log(self.ws, "daemon up")
        # warm rust-analyzer in the background (indexing is slow; debug works meanwhile)
        threading.Thread(target=self._warm_lsp, daemon=True).start()
        self.sock.settimeout(5.0)
        while not self._stop.is_set():
            if time.monotonic() - self._last > IDLE_SHUTDOWN_S:
                break
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                self._serve(conn)
        self._cleanup()

    def _warm_lsp(self) -> None:
        try:
            self.lsp = LspNav(self.ws)
            self.lsp.wait_ready(timeout=180)
            _log(self.ws, "rust-analyzer ready")
        except Exception as exc:  # noqa: BLE001
            self._lsp_error = str(exc)
            _log(self.ws, f"rust-analyzer failed: {exc}")

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(180)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            resp = self._dispatch(json.loads(buf.split(b"\n", 1)[0]))
        except Exception as exc:  # noqa: BLE001
            resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            conn.sendall((json.dumps(resp) + "\n").encode())
        except OSError:
            pass

    # -- command handling ------------------------------------------------------ #

    def _stop_summary(self, stop) -> dict:
        if stop.exited:
            out = "".join(self.session.output) if self.session else ""
            self.session = None
            return {"exited": True, "exit_code": stop.exit_code,
                    "output": out[-2000:] if out else ""}
        top = stop.top
        s = self.session
        return {"exited": False, "reason": stop.reason,
                "frame": f"{top.name}  {top.file}:{top.line}" if top else "?",
                "thread": stop.thread_id,
                "source": s.source_around() if s else "",
                "watches": s.watches_text() if (s and s.watches) else ""}

    def _bp_line(self, b: dict) -> str:
        k = b.get("kind")
        state = "" if b.get("enabled", True) else " [disabled]"
        if k == "line":
            extra = "".join(f" {t}={b[t]}" for t in ("condition", "hit", "log") if b.get(t))
            return f"  [{b['id']}] {Path(b['file']).name}:{b['line']}{extra}{state}"
        if k == "fn":
            return f"  [{b['id']}] fn {b['name']}{state}"
        if k == "watch":
            return f"  [{b['id']}] watch {b['name']}{state}"
        if k == "panic":
            return f"  [panic] rust panic{state}"
        return f"  [{b.get('id')}] {b}"

    def _dispatch(self, req: dict) -> dict:
        cmd = req.get("cmd")
        self._last = time.monotonic()

        if cmd == "ping":
            return {"ok": True}
        if cmd == "shutdown":
            self._stop.set()
            return {"ok": True}
        if cmd == "status":
            s = self.session
            return {"ok": True, "session": bool(s),
                    "stopped": bool(s and s.last_stop and not s.last_stop.exited),
                    "lsp_ready": bool(self.lsp and self.lsp.ready),
                    "cur_thread": s.cur_thread if s else None,
                    "threads": len(s.threads) if s else 0,
                    "breakpoints": len(s.all_bps()) if s else 0}

        # -- session setup --
        if cmd == "launch":
            if self.session:
                self.session.disconnect()
            self.session = DebugSession(req["program"], cwd=req.get("cwd"),
                                        args=req.get("args") or [])
            for b in req.get("breakpoints", []):
                self.session.add_line_bp(b["file"], int(b["line"]), condition=b.get("condition"),
                                         hit=b.get("hit"), log=b.get("log"))
            for name in req.get("fn_breaks", []):
                self.session.add_fn_bp(name)
            if req.get("panic"):
                self.session.add_panic_bp()
            self.session.launch()
            return {"ok": True, "stop": self._stop_summary(self.session.run())}

        # -- everything else needs a live session --
        s = self.session
        if cmd not in ("where", "def", "refs", "hover") and s is None:
            return {"ok": False, "error": "no debug session (run `rdbg launch` first)"}

        if cmd == "bp_add":
            b = s.add_line_bp(req["file"], int(req["line"]), condition=req.get("condition"),
                              hit=req.get("hit"), log=req.get("log"))
            return {"ok": True, "id": b["id"], "verified": b.get("verified", True)}
        if cmd == "bp_fn":
            return {"ok": True, "id": s.add_fn_bp(req["name"])["id"]}
        if cmd == "bp_panic":
            s.add_panic_bp()
            return {"ok": True, "id": "panic"}
        if cmd == "bp_watch":
            try:
                return {"ok": True, "id": s.add_watchpoint(req["var"])["id"]}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)}
        if cmd == "bp_list":
            return {"ok": True, "breakpoints": "\n".join(self._bp_line(b) for b in s.all_bps())
                    or "  (no breakpoints)"}
        if cmd == "bp_rm":
            bid = req["id"] if req["id"] == "panic" else int(req["id"])
            removed = s.remove_bp(bid)
            return {"ok": removed, "error": None if removed else "breakpoint not found"}
        if cmd == "bp_enable":
            bid = req["id"] if req["id"] == "panic" else int(req["id"])
            return {"ok": s.set_enabled(bid, bool(req["enabled"]))}

        if cmd == "continue":
            return {"ok": True, "stop": self._stop_summary(s.cont())}
        if cmd == "step":
            k = req.get("kind", "over")
            fn = {"over": lambda: s.step_over(), "in": s.step_in, "out": s.step_out,
                  "insn": lambda: s.step_over(insn=True)}.get(k, lambda: s.step_over())
            return {"ok": True, "stop": self._stop_summary(fn())}
        if cmd == "until":
            return {"ok": True, "stop": self._stop_summary(s.until(req["file"], int(req["line"])))}
        if cmd == "pause":
            return {"ok": True, "stop": self._stop_summary(s.pause())}
        if cmd == "restart":
            return {"ok": True, "stop": self._stop_summary(s.restart())}

        if cmd == "threads":
            s._refresh_threads()  # re-enumerate (lldb-dap can lag at a stop)
            return {"ok": True, "threads": s.threads_text()}
        if cmd == "thread":
            s._refresh_threads()
            return {"ok": s.select_thread(int(req["id"])),
                    "stop": self._stop_summary(s.last_stop) if s.last_stop else None}
        if cmd == "frame":
            ok = (s.frame_shift(1) if req.get("dir") == "up" else
                  s.frame_shift(-1) if req.get("dir") == "down" else
                  s.select_frame(int(req["index"])))
            return {"ok": ok, "source": s.source_around() if ok else "",
                    "vars": s.locals_text() if ok else ""}

        if cmd == "vars":
            return {"ok": True, "vars": s.locals_text(depth=int(req.get("depth", 3)))}
        if cmd == "eval":
            return {"ok": True, "value": s.evaluate(req["expr"])}
        if cmd == "set":
            return {"ok": True, "value": s.set_variable(req["path"], req["value"])}
        if cmd == "watch_expr":
            action = req.get("action")
            if action == "add":
                s.watches.append(req["expr"])
            elif action == "rm" and req.get("expr") in s.watches:
                s.watches.remove(req["expr"])
            return {"ok": True, "watches": s.watches_text()}
        if cmd == "bt":
            return {"ok": True, "bt": s.backtrace_text()}
        if cmd == "list":
            return {"ok": True, "source": s.source_around(radius=int(req.get("radius", 6)))}
        if cmd == "state":
            return {"ok": True,
                    "stop": self._stop_summary(s.last_stop) if s.last_stop else None,
                    "vars": s.locals_text(), "watches": s.watches_text() if s.watches else ""}
        if cmd == "stop":
            s.disconnect()
            self.session = None
            return {"ok": True, "stopped_session": True}

        # -- static nav (rust-analyzer) --
        if cmd in ("where", "def", "refs", "hover"):
            if self.lsp is None:
                return {"ok": False, "error": self._lsp_error
                        or "rust-analyzer still indexing; retry shortly"}
            self.lsp.wait_ready(timeout=30)
            if cmd == "where":
                return {"ok": True, "symbols": self.lsp.symbols(req["query"])}
            f, line, col = req["file"], int(req["line"]), int(req["col"])
            if cmd == "def":
                return {"ok": True, "locations": self.lsp.definition(f, line, col)}
            if cmd == "refs":
                return {"ok": True, "locations": self.lsp.references(f, line, col)}
            if cmd == "hover":
                return {"ok": True, "hover": self.lsp.hover(f, line, col)}

        return {"ok": False, "error": f"unknown cmd {cmd!r}"}

    def _cleanup(self) -> None:
        if self.session:
            self.session.disconnect()
        if self.lsp:
            self.lsp.shutdown()
        (self.state / "daemon.json").unlink(missing_ok=True)
        sp = getattr(self, "_sock_path", None)
        if sp is not None:
            Path(sp).unlink(missing_ok=True)
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        _log(self.ws, "daemon down")


def main() -> int:
    if not find_lldb_dap():
        sys.stderr.write("no lldb-dap adapter found\n")
        return 1
    ws = Path(sys.argv[sys.argv.index("--workspace") + 1]
              if "--workspace" in sys.argv else ".").resolve()
    Daemon(ws).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
