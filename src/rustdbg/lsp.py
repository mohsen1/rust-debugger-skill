"""Minimal rust-analyzer client for NAVIGATION (definition / hover / references /
workspace symbols) -- the static half of the IDE experience.

checkOnSave is off: we only want native, cargo-free navigation, kept warm by the
daemon so queries are fast after a one-time index. Stdlib only.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path


def find_rust_analyzer() -> str | None:
    try:
        proc = subprocess.run(["rustup", "which", "rust-analyzer"],
                              capture_output=True, text=True, timeout=15)
        cand = proc.stdout.strip()
        if proc.returncode == 0 and cand and Path(cand).exists():
            return cand
    except (OSError, subprocess.SubprocessError):
        pass
    return shutil.which("rust-analyzer")


def _uri(p: Path) -> str:
    return "file://" + str(p)


def _rel(uri: str, ws: Path) -> str:
    raw = uri.removeprefix("file://")
    try:
        return str(Path(raw).resolve().relative_to(ws))
    except ValueError:
        return raw


class LspNav:
    def __init__(self, workspace: Path) -> None:
        self.ws = Path(workspace).resolve()
        ra = find_rust_analyzer()
        if not ra:
            raise RuntimeError("rust-analyzer not found (rustup component add rust-analyzer)")
        # Own session/group so shutdown reaps rust-analyzer AND proc-macro-srv.
        self.proc = subprocess.Popen([ra], cwd=self.ws, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        self._id = 0
        self._lock = threading.Lock()
        self._replies: dict[int, queue.Queue] = {}
        self._opened: set[str] = set()
        self._indexing = set()
        self.ready = False
        threading.Thread(target=self._reader, daemon=True).start()
        self._initialize()

    # -- wire ------------------------------------------------------------------ #

    def _send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        with self._lock:
            self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
            self.proc.stdin.flush()

    def _request(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        self._id += 1
        rid = self._id
        q: queue.Queue = queue.Queue()
        self._replies[rid] = q
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return {}
        finally:
            self._replies.pop(rid, None)

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _reader(self) -> None:
        out = self.proc.stdout
        while True:
            line = out.readline()
            if not line:
                return
            if not line.lower().startswith(b"content-length:"):
                continue
            length = int(line.split(b":")[1])
            while line not in (b"\r\n", b"\n", b""):
                line = out.readline()
            try:
                msg = json.loads(out.read(length))
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                q = self._replies.get(msg["id"])
                if q:
                    q.put(msg)
            elif msg.get("method") == "$/progress":
                v = (msg.get("params") or {}).get("value") or {}
                tok = str((msg.get("params") or {}).get("token"))
                if v.get("kind") == "begin":
                    self._indexing.add(tok)
                elif v.get("kind") == "end":
                    self._indexing.discard(tok)
                    self.ready = True
            elif msg.get("method") in ("workspace/configuration",
                                       "window/workDoneProgress/create") and "id" in msg:
                n = len((msg.get("params") or {}).get("items", []))
                self._send({"jsonrpc": "2.0", "id": msg["id"],
                            "result": [None] * n if n else None})

    def _initialize(self) -> None:
        self._request("initialize", {
            "processId": os.getpid(), "rootUri": _uri(self.ws),
            "workspaceFolders": [{"uri": _uri(self.ws), "name": self.ws.name}],
            "capabilities": {"textDocument": {"hover": {"contentFormat": ["plaintext"]},
                                              "definition": {}, "references": {}},
                             "workspace": {"symbol": {}, "configuration": True,
                                           "workspaceFolders": True},
                             "window": {"workDoneProgress": True}},
            "initializationOptions": {"checkOnSave": False,
                                      "cargo": {"buildScripts": {"enable": True}},
                                      "procMacro": {"enable": True}},
        }, timeout=120)
        self._notify("initialized", {})

    def wait_ready(self, timeout: float = 120.0) -> bool:
        end = time.monotonic() + timeout
        # ready flips true when the first indexing pass ends; also settle briefly
        while time.monotonic() < end:
            if self.ready and not self._indexing:
                return True
            time.sleep(0.3)
        return self.ready

    def _open(self, rel: str) -> Path | None:
        p = (self.ws / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
        if not p.exists():
            return None
        key = str(p)
        if key not in self._opened:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            self._notify("textDocument/didOpen", {"textDocument": {
                "uri": _uri(p), "languageId": "rust", "version": 1, "text": text}})
            self._opened.add(key)
            time.sleep(0.3)  # let the file analyze
        return p

    # -- navigation ------------------------------------------------------------ #

    def _pos(self, rel: str, line: int, col: int):
        p = self._open(rel)
        if p is None:
            return None, None
        return p, {"textDocument": {"uri": _uri(p)},
                   "position": {"line": max(0, line - 1), "character": max(0, col - 1)}}

    def _loc(self, loc: dict) -> dict:
        uri = loc.get("uri") or loc.get("targetUri", "")
        rng = loc.get("range") or loc.get("targetSelectionRange") or {}
        s = rng.get("start") or {}
        return {"file": _rel(uri, self.ws), "line": int(s.get("line", 0)) + 1,
                "col": int(s.get("character", 0)) + 1}

    def definition(self, rel: str, line: int, col: int) -> list[dict]:
        p, params = self._pos(rel, line, col)
        if p is None:
            return []
        res = self._request("textDocument/definition", params).get("result") or []
        if isinstance(res, dict):
            res = [res]
        return [self._loc(x) for x in res]

    def references(self, rel: str, line: int, col: int, cap: int = 30) -> list[dict]:
        p, params = self._pos(rel, line, col)
        if p is None:
            return []
        params["context"] = {"includeDeclaration": False}
        res = self._request("textDocument/references", params).get("result") or []
        return [self._loc(x) for x in res[:cap]]

    def hover(self, rel: str, line: int, col: int) -> str:
        p, params = self._pos(rel, line, col)
        if p is None:
            return ""
        res = self._request("textDocument/hover", params).get("result") or {}
        contents = res.get("contents") or {}
        if isinstance(contents, dict):
            return contents.get("value", "")
        if isinstance(contents, list):
            return "\n".join(c.get("value", str(c)) if isinstance(c, dict) else str(c)
                             for c in contents)
        return str(contents)

    def symbols(self, query: str, cap: int = 30) -> list[dict]:
        res = self._request("workspace/symbol", {"query": query}).get("result") or []
        out = []
        for s in res[:cap]:
            d = self._loc(s.get("location") or {})
            d["name"] = s.get("name", "")
            d["container"] = s.get("containerName", "")
            out.append(d)
        return out

    def shutdown(self) -> None:
        try:
            self._request("shutdown", {}, timeout=5)
            self._notify("exit", {})
        except Exception:  # noqa: BLE001
            pass
        import os
        import signal
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
