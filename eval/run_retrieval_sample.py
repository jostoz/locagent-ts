"""Run a small rg/tgrep/LocAgent/hybrid agent matrix with one local model."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from eval.cline_runner import run_once
from eval.metrics import _load_jsonl
from eval.score import score_answer

HERE = Path(__file__).resolve().parent
BASE_SYSTEM = (HERE / "prompts" / "executor_system.txt").read_text(encoding="utf-8")
CONDITIONS = ("rg", "tgrep", "locagent", "hybrid")
ROUTING = {
    "rg": "Use native search/rg for repository search. LocAgent is unavailable. Do not call tgrep.",
    "tgrep": "LocAgent is unavailable. Use `tgrep -F -l PATTERN /workspace` to shortlist files, then `tgrep -F -n -m 20 PATTERN FILE` for bounded evidence. Use regex mode only when needed. Do not use rg or grep for repository search.",
    "locagent": "Use graph_search, graph_get, and graph_traverse for repository discovery. Do not call tgrep or rg for repository search.",
    "hybrid": "Route literal identifiers/strings/regex to bounded tgrep. Route intent to graph_search, entity bodies to graph_get, and callers/imports/renders to graph_traverse. For value conventions, use tgrep to enumerate exact checks and graph_get for relevant entities. Maximum four discovery calls per layer.",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def preindex(image: str, repo: Path) -> float:
    import time
    start = time.perf_counter()
    command = ["docker", "run", "--rm", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
               "--mount", f"type=bind,source={repo.resolve()},target=/workspace",
               "--entrypoint", "/usr/local/bin/tgrep", image, "index", "--force", "/workspace"]
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   text=True, encoding="utf-8", errors="replace")
    return round(time.perf_counter() - start, 3)


def count_search_calls(result) -> dict[str, int]:
    counts = {"tgrep": 0, "rg": 0, "graph": result.transcript.n_graph}
    for call in result.transcript.tool_calls:
        text = json.dumps(call.input, ensure_ascii=False) if not isinstance(call.input, str) else call.input
        counts["tgrep"] += int("tgrep " in text)
        counts["rg"] += int("rg " in text or "ripgrep" in text)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--tasks", default="q1,q3,q4,qC1")
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument("--model", default="qwen3.8-27b")
    ap.add_argument("--image", default="locagent-cline:stage1-v4")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--run-id", default="retrieval-sample-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    args = ap.parse_args()
    records = {r["id"]: r for r in _load_jsonl(str(HERE / "miro_clone_localization.jsonl"))}
    tasks, conditions = args.tasks.split(","), args.conditions.split(",")
    if set(conditions) - set(CONDITIONS): raise SystemExit("unknown condition")
    out = HERE / "results" / args.run_id; out.mkdir(parents=True, exist_ok=False)
    rows = []
    for condition in conditions:
        for task in tasks:
            record = records[task]
            worktree = out / "worktrees" / f"{condition}-{task}"
            worktree.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(args.repo), "worktree", "add", "--detach", str(worktree), record["pinned_at"]], check=True)
            index_s = preindex(args.image, worktree) if condition in ("tgrep", "hybrid") else 0
            result = run_once(task_id=task, condition=condition, prompt=record["prompt"], repo=str(worktree),
                run_path=str(out / f"{condition}-{task}.jsonl"), provider="openai-compatible", model=args.model,
                system=BASE_SYSTEM + "\n\nRETRIEVAL CONDITION\n" + ROUTING[condition], timeout_s=args.timeout,
                api_key="lm-studio-local", sandbox_image=args.image,
                disable_locagent=condition in ("rg", "tgrep"), expect_mcp=condition in ("locagent", "hybrid"))
            score, correct = score_answer(result.transcript.final_text, record["answer_tokens"])
            row = {"task": task, "condition": condition, "correct": correct, "score": score,
                   "wall_s": result.wall_s, "index_s": index_s, "iterations": result.transcript.iterations,
                   "native_calls": result.transcript.n_native, "input_tokens": result.transcript.usage.get("inputTokens", 0),
                   "output_tokens": result.transcript.usage.get("outputTokens", 0), "search_calls": count_search_calls(result),
                   "flake": result.flake, "final": result.transcript.final_text}
            rows.append(row); (out / f"{condition}-{task}.summary.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in row.items() if k != "final"}), flush=True)
    (out / "matrix.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
