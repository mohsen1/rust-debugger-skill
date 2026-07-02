#!/usr/bin/env python3
"""Benchmark coding agents on debugging tasks, with and without rdbg.

For each (task, agent, condition) it copies the task to a scratch dir, optionally
installs the rust-debugger skill, runs the agent headless on the task prompt,
times it, records token usage, and checks whether `cargo test` passes afterward.

Usage:
  python3 bench.py                      # all tasks, both agents, both conditions
  python3 bench.py --agents claude --tasks accumulator --repeat 2
  python3 bench.py --agents claude,codex --conditions with,without

Requires the agent CLIs on PATH (`claude`, `codex`) and — for the `with`
condition — `rdbg` (curl -fsSL https://azimi.me/rust-debugger-skill/install.sh | sh)
plus rust-analyzer and lldb-dap. Runs make real API calls; they cost money.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL = ROOT.parent / "skill" / "rust-debugger" / "SKILL.md"
RESULTS = ROOT / "results"

RDBG_NOTE = (
    "An `rdbg` debugger is available (the `rust-debugger` skill). When a value is "
    "wrong or a test panics, prefer breaking on the failing test and inspecting "
    "runtime values (`rdbg launch --cargo . --test <name> --break <file>:<line> -- "
    "<test>`, then `vars`/`eval`/`bt`) over adding prints and rebuilding.\n"
)


def install_rdbg(workdir: Path) -> None:
    """Set up the `with` condition: the skill + a pointer for both CLIs."""
    for rel in (".claude/skills/rust-debugger", ".agents/skills/rust-debugger"):
        d = workdir / rel
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(SKILL, d / "SKILL.md")
    (workdir / "CLAUDE.md").write_text(RDBG_NOTE)
    (workdir / "AGENTS.md").write_text(RDBG_NOTE)


def run_claude(workdir: Path, prompt: str) -> dict:
    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"],
        cwd=workdir, capture_output=True, text=True, timeout=900,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"tokens": None, "cost": None, "turns": None, "raw_error": proc.stdout[-500:] + proc.stderr[-500:]}
    u = data.get("usage", {})
    tokens = sum(v for k, v in u.items()
                 if k.endswith("_tokens") and isinstance(v, int))
    return {"tokens": tokens, "cost": data.get("total_cost_usd"), "turns": data.get("num_turns")}


def run_codex(workdir: Path, prompt: str) -> dict:
    proc = subprocess.run(
        ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", prompt],
        cwd=workdir, capture_output=True, text=True, timeout=900,
    )
    tokens = turns = None
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        # token usage events vary by codex version; grab the last totals we see
        usage = ev.get("usage") or ev.get("token_usage") or ev.get("info", {}).get("total_token_usage")
        if isinstance(usage, dict):
            t = sum(v for k, v in usage.items() if "token" in k and isinstance(v, int))
            if t:
                tokens = t
    return {"tokens": tokens, "cost": None, "turns": turns}


AGENTS = {"claude": run_claude, "codex": run_codex}


def cargo_test_passes(workdir: Path) -> bool:
    r = subprocess.run(["cargo", "test"], cwd=workdir, capture_output=True, text=True, timeout=600)
    return r.returncode == 0


def one_run(task: str, agent: str, condition: str, idx: int) -> dict:
    src = ROOT / "tasks" / task
    work = RESULTS / "work" / f"{task}-{agent}-{condition}-{idx}"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(src, work)
    (work / "target").exists() and shutil.rmtree(work / "target")
    if condition == "with":
        install_rdbg(work)
    prompt = (src / "PROMPT.md").read_text()

    env = dict(os.environ, PATH=f"{Path.home()}/.local/bin:" + os.environ["PATH"])
    os.environ.update(env)
    start = time.monotonic()
    try:
        info = AGENTS[agent](work, prompt)
        err = None
    except subprocess.TimeoutExpired:
        info, err = {"tokens": None, "cost": None, "turns": None}, "timeout"
    wall = time.monotonic() - start
    passed = False if err else cargo_test_passes(work)
    return {"task": task, "agent": agent, "condition": condition, "run": idx,
            "wall_s": round(wall, 1), "passed": passed, "error": err, **info}


def summarize(rows: list[dict]) -> None:
    print("\n=== raw runs ===")
    print(f"{'task':<14}{'agent':<8}{'cond':<9}{'pass':<6}{'wall_s':<9}{'tokens':<9}{'cost$':<8}{'turns'}")
    for r in rows:
        print(f"{r['task']:<14}{r['agent']:<8}{r['condition']:<9}"
              f"{str(r['passed']):<6}{str(r['wall_s']):<9}{str(r['tokens'] or '-'):<9}"
              f"{str(round(r['cost'],3) if r['cost'] else '-'):<8}{r['turns'] or '-'}")

    print("\n=== with vs without (means) ===")
    print(f"{'agent':<8}{'cond':<9}{'pass_rate':<11}{'wall_s':<9}{'tokens':<9}{'cost$'}")
    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(statistics.mean(xs), 1) if xs else None
    for agent in sorted({r["agent"] for r in rows}):
        for cond in ("without", "with"):
            g = [r for r in rows if r["agent"] == agent and r["condition"] == cond]
            if not g:
                continue
            pr = round(sum(r["passed"] for r in g) / len(g), 2)
            print(f"{agent:<8}{cond:<9}{str(pr):<11}{str(mean([r['wall_s'] for r in g])):<9}"
                  f"{str(mean([r['tokens'] for r in g]) or '-'):<9}{mean([r['cost'] for r in g]) or '-'}")


def main() -> None:
    all_tasks = ",".join(sorted(p.name for p in (ROOT / "tasks").iterdir() if p.is_dir()))
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=all_tasks, help="comma-separated; default: all")
    ap.add_argument("--agents", default="claude,codex")
    ap.add_argument("--conditions", default="without,with")
    ap.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)

    rows = []
    for task in a.tasks.split(","):
        for agent in a.agents.split(","):
            if not shutil.which(agent):
                print(f"skip {agent}: not on PATH", file=sys.stderr)
                continue
            for cond in a.conditions.split(","):
                for i in range(a.repeat):
                    print(f"running {task} / {agent} / {cond} #{i} …", file=sys.stderr, flush=True)
                    row = one_run(task, agent, cond, i)
                    print(f"  -> passed={row['passed']} wall={row['wall_s']}s tokens={row['tokens']}", file=sys.stderr)
                    rows.append(row)
    (RESULTS / "runs.json").write_text(json.dumps(rows, indent=2))
    summarize(rows)


if __name__ == "__main__":
    main()
