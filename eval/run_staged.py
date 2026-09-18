"""Run a frozen DeepSeek plan as isolated Cline executor steps.

The planner is deliberately outside this runner: create and validate its JSON
first with ``python -m eval.planner``.  This command then records a manifest
and one durable result per step, so a failed invocation can resume without
rerunning prior successful steps.

Raw Cline JSON streams may contain hidden reasoning.  They are scrubbed before
being retained; the result records keep only tool/count/latency metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from eval.cline_runner import run_once
from eval.guard import analyze
from eval.planner import load_plan, validate_plan
from eval.render_step import append_carry, load_carry, render


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def _scrub_hidden_reasoning(value: Any) -> Any:
    """Remove chain-of-thought fields while retaining tool/result evidence."""
    if isinstance(value, list):
        return [_scrub_hidden_reasoning(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key.lower() in {"reasoning", "reasoning_content", "reasoningcontent"}:
            continue
        out[key] = _scrub_hidden_reasoning(item)
    return out


def scrub_jsonl(source: Path, destination: Path | None = None) -> None:
    """Write a sanitized JSONL copy; non-JSON diagnostics are retained verbatim."""
    destination = destination or source
    lines = []
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            lines.append(json.dumps(_scrub_hidden_reasoning(json.loads(line)), ensure_ascii=False))
        except json.JSONDecodeError:
            lines.append(line)
    destination.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _summary(result, guard) -> dict:
    tr = result.transcript
    return {
        "exit_code": result.exit_code,
        "wall_s": result.wall_s,
        "flake": result.flake,
        "finish_reason": tr.finish_reason,
        "iterations": tr.iterations,
        "ctx_peak": tr.ctx_peak,
        "tool_calls": len(tr.tool_calls),
        "graph_calls": tr.n_graph,
        "native_calls": tr.n_native,
        "shell_calls": tr.n_shell,
        "errors": tr.errors[-5:],
        "guard": guard.as_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", required=True, help="task id under eval/plans/")
    ap.add_argument("--repo", required=True, help="clean executor worktree")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--system", default="eval/prompts/executor_system.txt")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--data-dir", help="isolated Cline data directory, if configured")
    ap.add_argument("--final-tsc", action="store_true",
                    help="run the fixed TypeScript acceptance command after every step succeeds")
    args = ap.parse_args(argv)

    plan = load_plan(args.plan)
    problems = validate_plan(plan)
    if problems:
        ap.error("invalid frozen plan: " + "; ".join(problems))
    repo = str(Path(args.repo).resolve())
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    system = Path(args.system).read_text(encoding="utf-8")
    head = _git(repo, "rev-parse", "HEAD")
    manifest = {
        "task_id": plan["task_id"], "plan_sha256": _sha256_json(plan),
        "repo": repo, "repo_head": head, "model": args.model,
        "timeout_s": args.timeout, "system_sha256": hashlib.sha256(system.encode()).hexdigest(),
    }
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
        ap.error("run directory belongs to another plan/revision/model; choose a new --run-dir")
    _write_json(manifest_path, manifest)

    carry_path = run_dir / "carry.json"
    for step in plan["steps"]:
        n = step["n"]
        result_path = run_dir / f"step-{n:02d}.result.json"
        if result_path.exists():
            print(f"step {n}: already recorded; resuming")
            continue
        prompt = render(plan, n, load_carry(str(carry_path)))
        prompt_path = run_dir / f"step-{n:02d}.prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        raw_path = run_dir / f"step-{n:02d}.jsonl"
        private_path = run_dir / f"step-{n:02d}.private.tmp.jsonl"
        try:
            result = run_once(task_id=plan["task_id"], condition="deepseek-plan-local-executor",
                              step=n, prompt=prompt, repo=repo, run_path=str(private_path),
                              model=args.model, system=system, timeout_s=args.timeout,
                              data_dir=args.data_dir)
            scrub_jsonl(private_path, raw_path)
        finally:
            private_path.unlink(missing_ok=True)
        report = analyze(result.transcript, repo=repo, target_file=step["target_file"],
                         expect_edit=step["operation"] == "edit", baseline_ref=None)
        saved = _summary(result, report)
        saved.update({"step": n, "target_file": step["target_file"],
                      "operation": step["operation"], "raw_log": raw_path.name})
        _write_json(result_path, saved)
        print(json.dumps(saved, ensure_ascii=False))
        done = result.flake == "ok" and "STEP DONE" in result.transcript.final_text
        if not done:
            print(f"step {n}: stopped; rerun the same command to resume after repairing it", file=sys.stderr)
            return 1
        append_carry(str(carry_path), n, [step["target_file"]],
                     f"completed; wall_s={result.wall_s}; finish={result.transcript.finish_reason}")

    if args.final_tsc:
        cmd = ["npx", "tsc", "-p", "tsconfig.app.json", "--noEmit"]
        check = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=180)
        _write_json(run_dir / "final-acceptance.json", {
            "cmd": cmd, "exit_code": check.returncode,
            "stdout_tail": check.stdout[-2000:], "stderr_tail": check.stderr[-2000:],
        })
        return check.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
