"""rdbg CLI -- IDE-like debugging for agents (rust-analyzer + lldb-dap).

LAUNCH
  rdbg launch --cargo <dir> [--bin <t>|--test <t>] --break f.rs:L [...] [-- ARGS]
  rdbg launch --bin-path <path> --break f.rs:L [...]
     optional at launch: --break-fn <name>   --panic

BREAKPOINTS  (set/change any time, even while paused)
  rdbg break <f.rs:line> [--if <expr>] [--hit <N>] [--log <msg>]
  rdbg break --fn <name>          break entering a function
  rdbg break --panic              break where a Rust panic is raised
  rdbg watch <var>                data breakpoint: break when a local changes
  rdbg breaks                     list all breakpoints (with ids)
  rdbg break-rm <id|panic>        remove a breakpoint
  rdbg break-off <id> / break-on <id>   disable / enable

RUN CONTROL
  rdbg run | continue             resume to the next stop
  rdbg step [over|in|out|insn]    step (source line, or one instruction)
  rdbg until <f.rs:line>          run to a line
  rdbg pause                      interrupt a running program
  rdbg restart                    relaunch with the same breakpoints

THREADS / FRAMES
  rdbg threads                    list threads (all stop together)
  rdbg thread <id>                switch the current thread
  rdbg frame <n> | up | down      select a stack frame (vars/eval follow it)
  rdbg bt                         backtrace of the current thread

INSPECT / MUTATE
  rdbg vars [--depth N]           locals with real Rust values
  rdbg eval <path>                evaluate a variable path (foo.bar[2].x)
  rdbg set <path> = <value>       change a variable's value
  rdbg watch-expr add|rm <path>   watch a value; shown at every stop
  rdbg list [--radius N]          source around the current line
  rdbg state                      stop + locals + watches in one shot

NAVIGATE (rust-analyzer)
  rdbg where <Name>               find a fn/type across the workspace
  rdbg def|hover|refs <f> <line> <col>

  rdbg status | stop | down
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

STATE = ".rustdbg"


def ws_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    return Path(out.stdout.strip()) if out.returncode == 0 else Path.cwd()


def request(ws: Path, payload: dict, timeout: float = 300.0) -> dict | None:
    addr = ws / STATE / "daemon.json"
    if not addr.exists():
        return None
    try:
        meta = json.loads(addr.read_text())
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(meta["socket"])
        s.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
        s.close()
        return json.loads(buf.split(b"\n", 1)[0]) if buf else None
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def ensure_daemon(ws: Path) -> None:
    if request(ws, {"cmd": "ping"}, timeout=3):
        return
    (ws / STATE).mkdir(exist_ok=True)
    starting = ws / STATE / "starting.pid"
    if starting.exists():
        try:
            os.kill(int(starting.read_text()), 0)
            return
        except (ValueError, ProcessLookupError, OSError):
            pass
    proc = subprocess.Popen(
        [sys.executable, "-m", "rustdbg.daemon", "--workspace", str(ws)], cwd=ws,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    try:
        starting.write_text(str(proc.pid))
    except OSError:
        pass
    for _ in range(40):
        if request(ws, {"cmd": "ping"}, timeout=1):
            return
        time.sleep(0.25)


def cargo_build(manifest: Path, bin_t: str | None, test_t: str | None) -> Path:
    cmd = ["cargo", "test" if test_t else "build", "--message-format=json"]
    if test_t:
        cmd += ["--no-run"] + (["--lib"] if test_t == "lib" else ["--test", test_t])
    if bin_t:
        cmd += ["--bin", bin_t]
    print("building ...", file=sys.stderr, flush=True)
    proc = subprocess.run(cmd, cwd=manifest, capture_output=True, text=True)
    exe = None
    for line in proc.stdout.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("reason") == "compiler-artifact" and m.get("executable"):
            name = (m.get("target") or {}).get("name", "")
            if (bin_t and name == bin_t) or (test_t and (test_t == "lib" or name == test_t)):
                exe = Path(m["executable"])
            elif not exe:
                exe = Path(m["executable"])
    if exe is None:
        sys.stderr.write(proc.stderr[-2000:])
        sys.exit("build failed / no executable produced")
    return exe


def print_stop(stop: dict | None) -> None:
    if not stop:
        print("(no stop -- not paused)")
        return
    if stop.get("exited"):
        print(f">>> program exited (code {stop.get('exit_code')})")
        if stop.get("output"):
            print("--- program output ---\n" + stop["output"].rstrip())
        return
    print(f">>> STOP [{stop.get('reason')}] {stop.get('frame')}  (thread {stop.get('thread')})")
    if stop.get("source"):
        print(stop["source"])
    if stop.get("watches"):
        print("watches:\n" + stop["watches"])


def parse_bp(spec: str, base: Path) -> tuple[str, int]:
    if ":" not in spec:
        sys.exit(f"bad breakpoint {spec!r} (want file.rs:line)")
    f, line = spec.rsplit(":", 1)
    p = Path(f)
    if not p.is_absolute():
        p = (base / f).resolve()
    return str(p), int(line)


def _opt(rest: list[str], flag: str) -> str | None:
    return rest[rest.index(flag) + 1] if flag in rest else None


def _opt_multi(rest: list[str], flag: str) -> str | None:
    """Value that may be several shell tokens (an unquoted expression/message):
    everything after ``flag`` up to the next ``--option`` or end."""
    if flag not in rest:
        return None
    i = rest.index(flag) + 1
    toks: list[str] = []
    while i < len(rest) and not rest[i].startswith("--"):
        toks.append(rest[i])
        i += 1
    return " ".join(toks) or None


def do_launch(ws: Path, rest: list[str]) -> int:
    cargo = binpath = bin_t = test_t = None
    breaks, fn_breaks, args = [], [], []
    panic = False
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--cargo": cargo = rest[i + 1]; i += 2
        elif a == "--bin-path": binpath = rest[i + 1]; i += 2
        elif a == "--bin": bin_t = rest[i + 1]; i += 2
        elif a == "--test": test_t = rest[i + 1]; i += 2
        elif a == "--break": breaks.append(rest[i + 1]); i += 2
        elif a == "--break-fn": fn_breaks.append(rest[i + 1]); i += 2
        elif a == "--panic": panic = True; i += 1
        elif a == "--": args = rest[i + 1:]; break
        else: sys.exit(f"unknown launch arg {a!r}")
    if not (breaks or fn_breaks or panic):
        sys.exit("launch needs at least one --break / --break-fn / --panic")
    if cargo:
        manifest = Path(cargo).resolve()
        program = cargo_build(manifest, bin_t, test_t)
    elif binpath:
        program = Path(binpath).resolve()
    else:
        sys.exit("launch needs --cargo <dir> or --bin-path <path>")
    bps = [{"file": p, "line": ln} for p, ln in (parse_bp(b, Path.cwd()) for b in breaks)]
    print(f"debugging {program.name}", file=sys.stderr)
    r = request(ws, {"cmd": "launch", "program": str(program), "cwd": str(program.parent),
                     "args": args, "breakpoints": bps, "fn_breaks": fn_breaks, "panic": panic})
    if not r or not r.get("ok"):
        sys.exit(f"launch failed: {(r or {}).get('error')}")
    print_stop(r.get("stop"))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    ws = ws_root()

    if cmd == "down":
        request(ws, {"cmd": "shutdown"}, timeout=5)
        print("rustdbg: daemon stopped")
        return 0
    ensure_daemon(ws)

    if cmd == "status":
        print(json.dumps(request(ws, {"cmd": "status"}) or {}, indent=2))
        return 0
    if cmd == "launch":
        return do_launch(ws, rest)

    # breakpoints
    if cmd == "break":
        if "--fn" in rest:
            r = request(ws, {"cmd": "bp_fn", "name": _opt(rest, "--fn")}) or {}
            print(f"fn breakpoint [{r.get('id')}]" if r.get("ok") else r.get("error"))
        elif "--panic" in rest:
            request(ws, {"cmd": "bp_panic"})
            print("panic breakpoint [panic] set (breaks where a Rust panic is raised)")
        else:
            f, line = parse_bp(rest[0], Path.cwd())
            hit = _opt(rest, "--hit")
            r = request(ws, {"cmd": "bp_add", "file": f, "line": line,
                             "condition": _opt_multi(rest, "--if"),
                             "hit": int(hit) if hit else None,
                             "log": _opt_multi(rest, "--log")}) or {}
            v = "" if r.get("verified", True) else " (UNVERIFIED -- no code at that line?)"
            print(f"breakpoint [{r.get('id')}] {rest[0]}{v}" if r.get("ok") else r.get("error"))
        return 0
    if cmd == "watch":
        r = request(ws, {"cmd": "bp_watch", "var": rest[0]}) or {}
        print(f"watchpoint [{r.get('id')}] on {rest[0]} (breaks when it changes)"
              if r.get("ok") else "error: " + str(r.get("error")))
        return 0
    if cmd == "breaks":
        r = request(ws, {"cmd": "bp_list"}) or {}
        print(r.get("breakpoints") if r.get("ok") else r.get("error"))
        return 0
    if cmd in ("break-rm", "break-on", "break-off"):
        if cmd == "break-rm":
            r = request(ws, {"cmd": "bp_rm", "id": rest[0]}) or {}
        else:
            r = request(ws, {"cmd": "bp_enable", "id": rest[0], "enabled": cmd == "break-on"}) or {}
        print("ok" if r.get("ok") else "not found")
        return 0

    # run control
    if cmd in ("run", "continue"):
        _stop(request(ws, {"cmd": "continue"}))
        return 0
    if cmd == "step":
        _stop(request(ws, {"cmd": "step", "kind": rest[0] if rest else "over"}))
        return 0
    if cmd == "until":
        f, line = parse_bp(rest[0], Path.cwd())
        _stop(request(ws, {"cmd": "until", "file": f, "line": line}))
        return 0
    if cmd == "pause":
        _stop(request(ws, {"cmd": "pause"}))
        return 0
    if cmd == "restart":
        _stop(request(ws, {"cmd": "restart"}))
        return 0

    # threads / frames
    if cmd == "threads":
        r = request(ws, {"cmd": "threads"}) or {}
        print(r.get("threads") if r.get("ok") else r.get("error"))
        return 0
    if cmd == "thread":
        r = request(ws, {"cmd": "thread", "id": rest[0]}) or {}
        print_stop(r.get("stop")) if r.get("ok") else print("no such thread")
        return 0
    if cmd in ("frame", "up", "down"):
        payload = ({"cmd": "frame", "dir": cmd} if cmd in ("up", "down")
                   else {"cmd": "frame", "index": rest[0]})
        r = request(ws, payload) or {}
        if r.get("ok"):
            print(r.get("source", "")); print("locals:"); print(r.get("vars", ""))
        else:
            print("no such frame")
        return 0

    # inspect / mutate
    if cmd == "vars":
        depth = _opt(rest, "--depth")
        r = request(ws, {"cmd": "vars", "depth": int(depth) if depth else 3}) or {}
        print(r.get("vars") if r.get("ok") else "error: " + str(r.get("error")))
        return 0
    if cmd == "eval":
        r = request(ws, {"cmd": "eval", "expr": rest[0]}) or {}
        print(r.get("value") if r.get("ok") else "error: " + str(r.get("error")))
        return 0
    if cmd == "set":
        # rdbg set <path> = <value>   (also accepts: rdbg set <path> <value>)
        joined = " ".join(rest)
        if "=" in joined:
            path, value = (x.strip() for x in joined.split("=", 1))
        else:
            path, value = rest[0], " ".join(rest[1:])
        r = request(ws, {"cmd": "set", "path": path, "value": value}) or {}
        print(r.get("value") if r.get("ok") else "error: " + str(r.get("error")))
        return 0
    if cmd == "watch-expr":
        action = rest[0] if rest and rest[0] in ("add", "rm") else "list"
        expr = " ".join(rest[1:]) if action in ("add", "rm") else None
        r = request(ws, {"cmd": "watch_expr", "action": action, "expr": expr}) or {}
        print(r.get("watches") if r.get("ok") else r.get("error"))
        return 0
    if cmd == "bt":
        r = request(ws, {"cmd": "bt"}) or {}
        print(r.get("bt") if r.get("ok") else "error: " + str(r.get("error")))
        return 0
    if cmd == "list":
        radius = _opt(rest, "--radius")
        r = request(ws, {"cmd": "list", "radius": int(radius) if radius else 6}) or {}
        print(r.get("source") if r.get("ok") else "error: " + str(r.get("error")))
        return 0
    if cmd == "state":
        r = request(ws, {"cmd": "state"}) or {}
        print_stop(r.get("stop")); print("locals:"); print(r.get("vars", ""))
        if r.get("watches"): print("watches:\n" + r["watches"])
        return 0
    if cmd == "stop":
        request(ws, {"cmd": "stop"})
        print("debug session ended")
        return 0

    # navigate
    if cmd == "where":
        r = request(ws, {"cmd": "where", "query": rest[0]}) or {}
        if not r.get("ok"):
            sys.exit(r.get("error"))
        for s in r.get("symbols", []):
            c = f" ({s['container']})" if s.get("container") else ""
            print(f"  {s['name']}{c}  {s['file']}:{s['line']}")
        return 0
    if cmd in ("def", "refs", "hover"):
        f, line, col = rest[0], int(rest[1]), int(rest[2])
        r = request(ws, {"cmd": cmd, "file": f, "line": line, "col": col}) or {}
        if not r.get("ok"):
            sys.exit(r.get("error"))
        if cmd == "hover":
            print(r.get("hover") or "(no hover)")
        else:
            for loc in r.get("locations", []) or ["(none)"]:
                print(f"  {loc['file']}:{loc['line']}:{loc['col']}" if isinstance(loc, dict) else loc)
        return 0

    sys.exit(__doc__)


def _stop(r: dict | None) -> None:
    if not r or not r.get("ok"):
        print("error: " + str((r or {}).get("error")))
        return
    print_stop(r.get("stop"))


if __name__ == "__main__":
    raise SystemExit(main())
