#!/usr/bin/env python3
"""Larger-repo benchmark: real fixed bugs, reset to before the fix.

For each case (a merged bug-fix commit with a regression test) the harness resets
a dedicated tsz worktree to the fix's PARENT commit, overlays just the regression
test (and its Cargo.toml registration) from the fix, confirms the test is red,
then runs an agent to re-derive the fix — once without rdbg, once with it —
and records tokens, wall time, and whether the test goes green.

Cases live in results-repo/cases.json (produced by the case-mining workflow).
The worktree is ~/code/tsz-bench (create with:
  git -C ~/code/tsz worktree add --detach ~/code/tsz-bench origin/main).

  python3 bench_repo.py                       # all cases, claude, both conditions
  python3 bench_repo.py --agents claude --cases 3   # first 3 cases only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import time
from pathlib import Path

WT = Path.home() / "code" / "tsz-bench"
ROOT = Path(__file__).resolve().parent
SKILL = ROOT.parent / "skill" / "rust-debugger" / "SKILL.md"
RESULTS = ROOT / "results-repo"
TRANSCRIPTS = RESULTS / "transcripts"
# Builds go into a fixed-size disk image, so a broad `cargo build` on the
# ~1.7M-line workspace fails inside the image instead of filling the main disk.
# Set up with: hdiutil create -size 45g -type SPARSE -fs APFS -volname tsztgt
#              /tmp/tsztgt.sparseimage ; hdiutil attach /tmp/tsztgt.sparseimage
TARGET_DIR = "/Volumes/tsztgt/target"

BASE_PROMPT = """A regression test in this repository fails:

    cargo nextest run {filter}

Diagnose the root cause and fix the source so that test passes. Do NOT modify the
test file. Keep the change minimal and correct for the general case — this is a
real diagnostic/behavior regression; match TypeScript (`tsc`). The workspace is
large: only ever run the one narrowed test command above, never the full suite."""

RDBG_NOTE = """

You also have `rdbg`, a Rust debugger for this project (run `rdbg` for usage).
Every tsz diagnostic funnels through one sink — `push_diagnostic` — so for a
wrong / extra / missing diagnostic ("fingerprint") mismatch, TRACE it at runtime
instead of grepping:

  rdbg launch --cargo . --test <suite> --break-fn push_diagnostic -- <test_name>
  rdbg eval diag.code          # the code being emitted; not the one you want?
  rdbg continue ; rdbg eval diag.code   # …continue until it is
  rdbg bt                      # walks back to the exact decision that emitted it
  rdbg frame <n> ; rdbg vars   # at that frame, inspect the types/flags that produced it

For an EXTRA (false-positive) diagnostic the backtrace shows what decided to emit
it; for a WRONG value, inspect the source/target type being formatted; for a
MISSING one, break where the check should fire and see why its condition is false.
Prefer this over adding prints and rebuilding."""


def sh(args, timeout=None):
    return subprocess.run(args, cwd=WT, capture_output=True, text=True, timeout=timeout)


def reset_to(case):
    sh(["git", "reset", "--hard", case["parent"]])
    sh(["git", "clean", "-fd"])  # untracked non-ignored only; target/ is ignored
    files = list(case.get("test_files", [])) + list(case.get("cargo_files_to_checkout", []))
    if files:
        sh(["git", "checkout", case["sha"], "--", *files])


def verify(case, timeout=1500):
    r = sh(["cargo", "nextest", "run", *case["nextest_filter"].split()], timeout=timeout)
    return r.returncode == 0


def install_rdbg():
    d = WT / ".agents" / "skills" / "rust-debugger"
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(SKILL, d / "SKILL.md")


import threading

MIN_FREE_GB = 15  # kill a session if free disk drops below this (a runaway build)


def _run_watched(cmd, timeout=2700):
    """Run a subprocess with a disk watchdog: if free space on WT's volume drops
    below MIN_FREE_GB, kill the process tree (a thrashing agent that ran a broad
    `cargo build` on the ~1.7M-line workspace can fill the disk in one session)."""
    proc = subprocess.Popen(cmd, cwd=WT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    killed = {"disk": False}

    def watch():
        while proc.poll() is None:
            if shutil.disk_usage(WT).free / 1e9 < MIN_FREE_GB:
                killed["disk"] = True
                subprocess.run(["pkill", "-9", "-f", "cargo"], capture_output=True)
                subprocess.run(["pkill", "-9", "-f", "rustc"], capture_output=True)
                proc.kill()
                return
            time.sleep(5)

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out, killed["disk"]


def run_claude(prompt, tpath):
    out, disk_killed = _run_watched(
        ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"])
    TRANSCRIPTS.mkdir(exist_ok=True)
    tpath.write_text(out)  # full JSONL transcript: every assistant / tool_use / tool_result event
    if disk_killed:
        return {"tokens": None, "cost": None, "turns": None, "disk_killed": True}
    result = None
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            result = ev
    if not result:
        return {"tokens": None, "cost": None, "turns": None}
    u = result.get("usage", {})
    tokens = sum(v for k, v in u.items() if k.endswith("_tokens") and isinstance(v, int))
    return {"tokens": tokens, "cost": result.get("total_cost_usd"), "turns": result.get("num_turns")}


def run_codex(prompt, tpath):
    out, disk_killed = _run_watched(
        ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", prompt])
    TRANSCRIPTS.mkdir(exist_ok=True)
    tpath.write_text(out)
    if disk_killed:
        return {"tokens": None, "cost": None, "turns": None, "disk_killed": True}
    tokens = None
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = ev.get("usage") or ev.get("token_usage") or ev.get("info", {}).get("total_token_usage")
        if isinstance(usage, dict):
            t = sum(v for k, v in usage.items() if "token" in k and isinstance(v, int))
            if t:
                tokens = t
    return {"tokens": tokens, "cost": None, "turns": None}


AGENTS = {"claude": run_claude, "codex": run_codex}


def one_run(case, agent, cond):
    reset_to(case)
    baseline_red = not verify(case)  # the case must start failing
    prompt = BASE_PROMPT.format(filter=case["nextest_filter"])
    if cond == "with":
        install_rdbg()
        prompt += RDBG_NOTE
    os.environ["PATH"] = f"{Path.home()}/.local/bin:" + os.environ["PATH"]
    tpath = TRANSCRIPTS / f"{case['sha'][:10]}-{cond}.jsonl"
    start = time.monotonic()
    try:
        info = AGENTS[agent](prompt, tpath)
        err = "disk-killed" if info.pop("disk_killed", False) else None
    except subprocess.TimeoutExpired:
        info, err = {"tokens": None, "cost": None, "turns": None}, "timeout"
    wall = time.monotonic() - start
    passed = verify(case) if err is None else False
    subprocess.run(["pkill", "-f", "rdbg __daemon"], capture_output=True)
    subprocess.run(["pkill", "-f", "lldb-dap"], capture_output=True)
    return {"case": case["sha"][:10], "bug": case.get("bug", "")[:48], "agent": agent, "cond": cond,
            "baseline_red": baseline_red, "passed": passed, "wall_s": round(wall, 1), "error": err, **info}


def summarize(rows):
    print("\n=== runs ===")
    print(f"{'case':<12}{'agent':<8}{'cond':<9}{'red':<5}{'pass':<6}{'wall_s':<9}{'tokens':<9}{'bug'}")
    for r in rows:
        print(f"{r['case']:<12}{r['agent']:<8}{r['cond']:<9}{str(r['baseline_red'])[:1]:<5}"
              f"{str(r['passed']):<6}{str(r['wall_s']):<9}{str(r['tokens'] or '-'):<9}{r['bug']}")

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(statistics.mean(xs), 1) if xs else None
    print("\n=== with vs without (means over valid, red-baseline runs) ===")
    print(f"{'agent':<8}{'cond':<9}{'solved':<9}{'wall_s':<9}{'tokens'}")
    for agent in sorted({r["agent"] for r in rows}):
        for cond in ("without", "with"):
            g = [r for r in rows if r["agent"] == agent and r["cond"] == cond and r["baseline_red"]]
            if not g:
                continue
            solved = f"{sum(r['passed'] for r in g)}/{len(g)}"
            print(f"{agent:<8}{cond:<9}{solved:<9}{str(mean([r['wall_s'] for r in g])):<9}{mean([r['tokens'] for r in g]) or '-'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", default="claude")
    ap.add_argument("--conditions", default="without,with")
    ap.add_argument("--cases", type=int, default=0, help="limit to first N cases (0 = all)")
    a = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    cases = json.loads((RESULTS / "cases.json").read_text())
    if a.cases:
        cases = cases[: a.cases]
    if not WT.exists():
        raise SystemExit(f"worktree {WT} missing — see the module docstring")
    if not Path(TARGET_DIR).parent.exists():
        raise SystemExit(f"capped target volume {TARGET_DIR} not mounted — see the module docstring")
    # every cargo build (agent's and verify's) goes into the capped image
    os.environ["CARGO_TARGET_DIR"] = TARGET_DIR

    # resume: keep whatever already completed, skip those (case, cond) pairs
    existing = []
    if (RESULTS / "runs.json").exists():
        try:
            existing = json.loads((RESULTS / "runs.json").read_text())
        except json.JSONDecodeError:
            existing = []
    done = {(r["case"], r["cond"]) for r in existing}
    rows = list(existing)
    conds = a.conditions.split(",")
    for case in cases:
        if all((case["sha"][:10], c) in done for c in conds):
            continue
        # fresh capped-target budget per case (both conditions share one commit's build)
        subprocess.run(["rm", "-rf", TARGET_DIR], capture_output=True)
        for agent in a.agents.split(","):
            if not shutil.which(agent):
                continue
            for cond in conds:
                if (case["sha"][:10], cond) in done:
                    continue
                print(f"running {case['sha'][:10]} / {agent} / {cond} …", flush=True)
                row = one_run(case, agent, cond)
                print(f"  -> red={row['baseline_red']} passed={row['passed']} "
                      f"wall={row['wall_s']}s tokens={row['tokens']} {row.get('error') or ''}", flush=True)
                rows.append(row)
                (RESULTS / "runs.json").write_text(json.dumps(rows, indent=2))
    # restore worktree
    subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=WT, capture_output=True)
    subprocess.run(["git", "clean", "-fd"], cwd=WT, capture_output=True)
    summarize(rows)


if __name__ == "__main__":
    main()
