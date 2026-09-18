"""Translate a validated Stage-1 plan into PAIOS task packets.

The planner's target repository is ``miro-clone`` while PAIOS runs from this
repository.  Therefore a generated packet is deliberately scoped to local
evidence (the plan, transcript, score, and review), not to an unverifiable
claim that ``verify.sh`` can inspect another repository's Git diff.

One packet is emitted per planner step.  This is the useful unit of recovery:
each executor invocation has one target file and acceptance check, and a
failed step can be retried or reviewed without reopening the other steps.
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


def _task_id(plan_id: str, number: int) -> str:
    safe = _SAFE_ID.sub("-", plan_id).strip(".-") or "plan"
    return f"TASK-{safe}-step-{number:02d}"


def packet_for_step(plan: Mapping[str, object], step: Mapping[str, object]) -> str:
    """Render one portable, line-oriented PAIOS YAML packet.

    The shell implementation intentionally reads a small YAML subset, so the
    generated form avoids multiline scalars and keeps authority patterns on
    their own lines.
    """
    plan_id = str(plan["task_id"])
    n = int(step["n"])
    task_id = _task_id(plan_id, n)
    target = str(step["target_file"])
    operation = str(step["operation"])
    acceptance = step.get("acceptance_check") or {}
    command = acceptance.get("cmd", "") if isinstance(acceptance, Mapping) else ""
    goal = str(step["goal"])
    edit_spec = str(step.get("edit_spec") or "")
    tool = str(step.get("tool") or "")

    lines = [
        f"id: {task_id}",
        f"title: {_quote(f'{plan_id}: step {n} — {goal}')}",
        f"objective: {_quote(goal)}",
        "authority:",
        "  # PAIOS can only verify evidence committed in this repository.",
        "  can_modify:",
        "    - eval/**",
        "    - docs/**",
        "external_effect:",
        f"  repository: {_quote('C:/Users/joz/Documents/miro-clone')}",
        f"  target_file: {_quote(target)}",
        f"  operation: {_quote(operation)}",
        "  verification: human_review_required",
        "executor:",
        f"  tool: {_quote(tool)}",
        f"  edit_spec: {_quote(edit_spec)}",
        "acceptance:",
        f"  - {_quote('Run in miro-clone: ' + str(command))}" if command else
        f"  - {_quote('Inspect only; no command required by the planner')}",
        f"  - {_quote('Persist a local transcript or score reference under eval/results/ before review')}",
        f"  - {_quote('Human review must inspect the external miro-clone diff; verify.sh only verifies local PAIOS evidence')}",
        "priority: high",
        "status: pending",
        "source_plan:",
        f"  task_id: {_quote(plan_id)}",
        f"  step: {n}",
    ]
    return "\n".join(lines) + "\n"


def write_packets(plan: Mapping[str, object], out_dir: Path = _DEFAULT_OUT) -> list[Path]:
    """Validate *plan* and atomically write one packet per step."""
    problems = validate_plan(dict(plan))
    if problems:
        raise ValueError("invalid plan: " + "; ".join(problems))

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for step in plan["steps"]:  # validated above
        assert isinstance(step, Mapping)
        path = out_dir / f"{_task_id(str(plan['task_id']), int(step['n']))}.yaml"
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(packet_for_step(plan, step), encoding="utf-8")
        tmp.replace(path)
        paths.append(path)
    return paths


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("plan", type=Path, help="planner JSON file")
    ap.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT)
    args = ap.parse_args(list(argv) if argv is not None else None)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    for path in write_packets(plan, args.out_dir):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
