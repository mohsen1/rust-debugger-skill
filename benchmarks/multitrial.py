#!/usr/bin/env python3
"""Multi-trial a single tsz case: set up + build once, then run the agent N times per
condition on the cached build, to measure the MEDIAN (beat single-run variance)."""
import sys, json, shutil, subprocess, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_tsz as B

CASE_IDX = int(sys.argv[1]); N = int(sys.argv[2]) if len(sys.argv) > 2 else 3
IMAGE = sys.argv[3] if len(sys.argv) > 3 else "/Volumes/tszMT"
case = json.load(open(B.RESULTS / "cases-tsz.json"))[CASE_IDX]
sha = case["sha"][:10]
slot = Path(IMAGE) / "slot"
env = B.env_for(IMAGE)

def reap():
    for pat in ("rdbg __daemon", "lldb-dap", "codelldb", "debugserver"):
        subprocess.run(["pkill", "-9", "-f", pat], capture_output=True)

shutil.rmtree(f"{IMAGE}/target", ignore_errors=True)
B.setup_case(slot, case)
red = not B.verify(slot, IMAGE, case, env)
print(f"[{sha}] red-at-parent: {red}", flush=True)
if not red:
    print("NOT RED — abort"); sys.exit(1)
stem = case["test_stems"][0] if case["test_stems"] else ""
testfn = B.first_testfn(slot, case) or "<failing_test>"

res = {"without": [], "with": []}
for cond in ["without", "with"]:
    for t in range(N):
        B.reset_slot(slot)
        prompt = B.BASE_PROMPT.format(filter=case["nextest_filter"])
        if cond == "with":
            d = slot / ".agents" / "skills" / "rust-debugger"; d.mkdir(parents=True, exist_ok=True)
            shutil.copy(B.SKILL, d / "SKILL.md")
            prompt += B.RDBG_NOTE.format(stem=stem, testfn=testfn)
        info = B.run_agent(slot, IMAGE, prompt, Path(f"/tmp/mt-{sha}-{cond}-{t}.jsonl"), env, model="opus")
        reap()
        passed = B.verify(slot, IMAGE, case, env) if not info["disk_killed"] else False
        res[cond].append({"tok": info["tokens"], "passed": passed, "timed_out": info["timed_out"]})
        print(f"  {cond} trial {t}: tok={info['tokens']} passed={passed} wall={info['wall_s']}", flush=True)

json.dump(res, open(f"/tmp/mt-{sha}-results.json", "w"), indent=2)
wo = [r["tok"] for r in res["without"] if r["tok"]]
wi = [r["tok"] for r in res["with"] if r["tok"]]
if wo and wi:
    mwo, mwi = statistics.median(wo), statistics.median(wi)
    print(f"\n[{sha}] WITHOUT median {mwo/1e6:.2f}M {[round(x/1e6,2) for x in wo]}")
    print(f"[{sha}] WITH    median {mwi/1e6:.2f}M {[round(x/1e6,2) for x in wi]}")
    print(f"[{sha}] MEDIAN Δ = {(mwi-mwo)/mwo*100:+.0f}%  ({'WASTE >100%' if (mwi-mwo)/mwo*100>100 else 'OK'})")
reap()
