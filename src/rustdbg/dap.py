"""Minimal Debug Adapter Protocol (DAP) client over stdio.

DAP framing is identical to LSP: ``Content-Length: N\\r\\n\\r\\n<json>``. Messages
are requests (client->adapter), responses (adapter->client, correlated by
``request_seq``), and events (adapter->client, e.g. ``stopped`` / ``exited``).

This is the low-level transport. :mod:`debug_session` wraps it into a
Rust-aware debugging session (launch a cargo binary under ``lldb-dap`` with the
rust value formatters, break, inspect, step).

Dependency-free (stdlib only) so it can run in the same lightweight process as
the rest of the in-workspace tooling.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Any


class DapError(RuntimeError):
    pass


class DapClient:
    """Talk to a Debug Adapter subprocess over stdio (framed JSON-RPC)."""

    def __init__(self, adapter_argv: list[str], env: dict[str, str] | None = None) -> None:
        # Own session/group so we can reap the adapter AND its debugserver child.
        self.proc = subprocess.Popen(
            adapter_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, start_new_session=True,
        )
        self._seq = 0
        self._replies: dict[int, queue.Queue] = {}
        self._events: queue.Queue = queue.Queue()
        self._alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    # -- wire ----------------------------------------------------------------- #

    def _reader(self) -> None:
        out = self.proc.stdout
        assert out is not None
        buf = b""
        while True:
            ch = out.read(1)
            if not ch:
                self._alive = False
                return
            buf += ch
            if buf.endswith(b"\r\n\r\n"):
                try:
                    headers = dict(
                        line.split(b": ", 1) for line in buf.strip().splitlines()
                    )
                    length = int(headers[b"Content-Length"])
                except (ValueError, KeyError):
                    buf = b""
                    continue
                body = out.read(length)
                buf = b""
                try:
                    msg = json.loads(body)
                except json.JSONDecodeError:
                    continue
                self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "response":
            q = self._replies.get(msg.get("request_seq"))
            if q is not None:
                q.put(msg)
        elif kind == "event":
            self._events.put(msg)
        elif kind == "request":
            # Reverse requests (e.g. runInTerminal) -- refuse politely so the
            # adapter does not hang waiting.
            self._respond(msg.get("seq"), msg.get("command"), success=False)

    def _respond(self, request_seq: int, command: str, success: bool) -> None:
        self._seq += 1
        payload = {"seq": self._seq, "type": "response", "request_seq": request_seq,
                   "success": success, "command": command}
        self._write(payload)

    def _write(self, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        assert self.proc.stdin is not None
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(raw) + raw)
        self.proc.stdin.flush()

    # -- requests / events ---------------------------------------------------- #

    def send(self, command: str, arguments: dict | None = None) -> int:
        """Send a request without blocking; return its seq (for :meth:`reply`)."""
        self._seq += 1
        seq = self._seq
        self._replies[seq] = queue.Queue()
        payload: dict[str, Any] = {"seq": seq, "type": "request", "command": command}
        if arguments is not None:
            payload["arguments"] = arguments
        self._write(payload)
        return seq

    def reply(self, seq: int, timeout: float = 30.0) -> dict:
        try:
            resp = self._replies[seq].get(timeout=timeout)
        except queue.Empty as exc:
            raise DapError(f"timed out waiting for reply to seq {seq}") from exc
        finally:
            self._replies.pop(seq, None)
        return resp

    def request(self, command: str, arguments: dict | None = None,
                timeout: float = 30.0) -> dict:
        """Send a request and block for its response."""
        resp = self.reply(self.send(command, arguments), timeout=timeout)
        if not resp.get("success", False):
            raise DapError(resp.get("message") or f"{command} failed")
        return resp

    def request_soft(self, command: str, arguments: dict | None = None,
                     timeout: float = 30.0) -> dict:
        """Like :meth:`request` but returns the (possibly failed) response."""
        return self.reply(self.send(command, arguments), timeout=timeout)

    def wait_event(self, name: str, timeout: float = 30.0) -> dict:
        """Block until an event named ``name`` arrives (draining others)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ev = self._events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if ev.get("event") == name:
                return ev
        raise DapError(f"event {name!r} not received within {timeout}s")

    def poll_event(self, timeout: float = 0.0) -> dict | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._alive = False
        import os
        import signal
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(self.proc.pid), sig)  # adapter + debugserver
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    self.proc.send_signal(sig)
                except Exception:  # noqa: BLE001
                    pass
            try:
                self.proc.wait(timeout=3)
                return
            except Exception:  # noqa: BLE001
                continue
