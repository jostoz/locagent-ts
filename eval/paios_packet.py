"""Translate a validated Stage-1 plan into a PAIOS task packet.

The planner's target repository is ``miro-clone`` while PAIOS runs from this
repository.  Therefore a generated packet is deliberately scoped to local
evidence (the plan, transcript, score, and review), not to an unverifiable
claim that ``verify.sh`` can inspect another repository's Git diff.

One packet is emitted per PLAN (i.e. per corpus task), not per step.

This reverses the original one-packet-per-step design after the opacity
stress test (``eval/results/ablation_stress/``, ``docs/TYPESCRIPT_PORT_PLAN.md``
"Stage 2" section, resolved 2026-09-17): a plan's steps are not independently
useful units of acceptance. The stress corpus's real failure mode was wiring
a change across multiple files (model field -> UI prop -> call site) where an
early step (e.g. "add the interface field") succeeds in isolation while a
later step (e.g. "wire the call site") is dropped, abandoned, or produces a
TypeScript error -- the overall feature stays broken even though the first
step's own ``acceptance_check`` passed. A packet-per-step design would accept
and close that first step as done, burying the wiring failure instead of
surfacing it. Gating acceptance on the plan's ``final_acceptance`` (which
runs after every step, over the whole worktree) instead of each step's own
``acceptance_check`` is what catches that failure mode -- it is the
regression test this module's test suite exercises.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Iterable, Mapping, Optional

from eval.planner import validate_plan

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "tasks"


def _quote(value: object) -> str:
    """Return a conservative YAML double-quoted scalar."""
    return json.dumps(str(value), ensure_ascii=False)


def _task_id(plan_id: str) -> str:
    safe = _SAFE_ID.sub("-", plan_id).strip(".-") or "plan"
    return f"TASK-{safe}"


def packet_for_plan(plan: Mapping[str, object]) -> str:
    """Render one portable, line-oriented PAIOS YAML packet for *plan* as a
    whole.

    The shell implementation intentionally reads a small YAML subset, so the
    generated form avoids multiline scalars and keeps authority patterns on
    their own lines. ``plan_steps`` is informational context for the human
    reviewer (and for a future automated re-check), never the acceptance gate
    itself -- see the module docstring for why.
    """
    plan_id = str(plan["task_id"])
    task_id = _task_id(plan_id)
    task_text = str(plan["task"])
    steps = list(plan["steps"])
    final = plan.get("final_acceptance") or {}
    final_cmd = final.get("cmd", "") if isinstance(final, Mapping) else ""
    target_files = list(dict.fromkeys(str(s["target_file"]) for s in steps))

    lines = [
        f"id: {task_id}",
        f"title: {_quote(f'{plan_id}: {task_text}')}",
        f"objective: {_quote(task_text)}",
        "authority:",
        "  # PAIOS can only verify evidence committed in this repository.",
        "  can_modify:",
        "    - eval/**",
        "    - docs/**",
        "external_effect:",
        f"  repository: {_quote('C:/Users/joz/Documents/miro-clone')}",
        "  target_files:",
    ]
    for tf in target_files:
        lines.append(f"    - {_quote(tf)}")
    lines += [
        "  verification: human_review_required",
        "plan_steps:",
    ]
    for step in steps:
        n = int(step["n"])
        lines += [
            f"  - n: {n}",
            f"    goal: {_quote(step['goal'])}",
            f"    target_file: {_quote(step['target_file'])}",
            f"    operation: {_quote(step['operation'])}",
        ]
    lines += [
        "acceptance:",
        f"  - {_quote('Run in miro-clone after every plan step completes: ' + str(final_cmd))}"
        if final_cmd else
        f"  - {_quote('Inspect only; no final_acceptance command required by the planner')}",
        "  - \"Persist a local transcript or score reference under eval/results/ before review\"",
        "  - \"Human review must inspect the external miro-clone diff AND confirm every\"",
        "  - \"plan step's target_file was actually touched/wired -- an individual step's own\"",
        "  - \"acceptance_check passing is not sufficient; only final_acceptance over the whole\"",
        "  - \"worktree, after all steps ran, counts as the task being done (see module docstring\"",
        "  - \"of eval/paios_packet.py: this is what the opacity stress test's wiring failure needs.)\"",
        "priority: high",
        "status: pending",
        "source_plan:",
        f"  task_id: {_quote(plan_id)}",
    ]
    return "\n".join(lines) + "\n"


def write_packet(plan: Mapping[str, object], out_dir: Path = _DEFAULT_OUT) -> Path:
    """Validate *plan* and atomically write its single packet."""
    problems = validate_plan(dict(plan))
    if problems:
        raise ValueError("invalid plan: " + "; ".join(problems))

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_task_id(str(plan['task_id']))}.yaml"
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(packet_for_plan(plan), encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("plan", type=Path, help="planner JSON file")
    ap.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT)
    args = ap.parse_args(list(argv) if argv is not None else None)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    print(write_packet(plan, args.out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
