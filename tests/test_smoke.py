"""Import + MCP-protocol smoke tests (no debug adapter required)."""
import json
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, SRC)


def test_imports():
    import rustdbg  # noqa: F401
    from rustdbg import cli, daemon, dap, lsp, mcp, session  # noqa: F401
    assert rustdbg.__version__


def test_short_type_and_fn():
    from rustdbg.session import short_fn, short_type
    assert short_type("alloc::vec::Vec<demo::Item, alloc::alloc::Global>") == "Vec<Item>"
    assert short_fn("demo::total::h71bcff9653903dac") == "demo::total"
    assert "<" in short_fn("core::ops::function::FnMut$LT$Args$GT$")


def test_bp_multi_arg_parse():
    from rustdbg.cli import _opt_multi
    assert _opt_multi(["f:1", "--if", "k", "==", "2", "--hit", "3"], "--if") == "k == 2"


def test_mcp_protocol():
    p = subprocess.Popen([sys.executable, "-m", "rustdbg.mcp"], cwd=SRC,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                         env={"PYTHONPATH": SRC, "PATH": __import__("os").environ["PATH"]})

    def rpc(obj):
        p.stdin.write(json.dumps(obj) + "\n"); p.stdin.flush()
        return json.loads(p.stdout.readline())

    try:
        init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {}}})
        assert init["result"]["serverInfo"]["name"] == "rustdbg"
        tl = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in tl["result"]["tools"]}
        assert {"debug_launch", "debug_eval", "debug_set", "debug_where"} <= names
        assert len(names) >= 20
    finally:
        p.stdin.close()
        p.terminate()


if __name__ == "__main__":
    test_imports(); test_short_type_and_fn(); test_bp_multi_arg_parse(); test_mcp_protocol()
    print("all smoke tests passed")
