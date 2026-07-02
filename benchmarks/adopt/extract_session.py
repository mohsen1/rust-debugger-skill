"""transcript.jsonl (stream-json) -> compact numbered session log for analysis."""
import json, sys
from pathlib import Path

def extract(path):
    out = []
    for line in open(path):
        try: ev = json.loads(line)
        except json.JSONDecodeError: continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list): continue
        for b in content:
            if not isinstance(b, dict): continue
            t = b.get("type")
            if t == "text":
                txt = b.get("text", "").strip()
                if txt: out.append(("THINK", txt[:700]))
            elif t == "tool_use":
                inp = b.get("input") or {}
                arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("old_string") or json.dumps(inp)[:140]
                out.append((f"TOOL {b.get('name')}", str(arg)[:220]))
            elif t == "tool_result":
                c = b.get("content")
                if isinstance(c, list): c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                out.append(("RESULT", str(c)[:260].replace("\n", " ⏎ ")))
    return out

SRC = Path("../results-adopt")
DST = SRC / "sessions"; DST.mkdir(exist_ok=True)
n = 0
for tp in sorted(SRC.glob("*/transcript.jsonl")):
    run = tp.parent.name
    steps = extract(tp)
    lines = [f"=== session: {run} ===", f"(steps: {len(steps)})", ""]
    for i, (kind, body) in enumerate(steps, 1):
        lines.append(f"[{i}] {kind}: {body}")
    text = "\n".join(lines)
    if len(text) > 45000: text = text[:45000] + "\n…(truncated)"
    (DST / f"{run}.txt").write_text(text)
    n += 1
print(f"extracted {n} session logs to {DST}")
