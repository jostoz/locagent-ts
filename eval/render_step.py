"""Render one plan step (+ carry-context from prior steps) into the prompt the
executor sees for a single Cline call. Only the current step is shown -- no
later-step leakage -- so the 9b cannot "help" by jumping ahead.

    prompt = render(plan, step_n=2, carry=load_carry('eval/results/.../e6.carry.json'))

Carry format (``eval/results/<run>/<task>.carry.json``, JSON list, appended to
after each step by ``run_matrix.py``):

    [{"step": 1, "resolved": ["src/board/useStickyNotes.ts:useStickyNotes"],
      "note": "StickyNote interface edited, tsc green"}, ...]

Capped to the last ``_CARRY_MAX_ITEMS`` entries / ``_CARRY_MAX_CHARS`` chars so
a long plan does not slowly refill the context it was designed to avoid.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

_CARRY_MAX_ITEMS = 5
_CARRY_MAX_CHARS = 800


def _format_carry(carry: List[dict]) -> str:
    if not carry:
        return '(none -- this is the first step)'
    items = carry[-_CARRY_MAX_ITEMS:]
    lines = []
    for c in items:
        resolved = ', '.join(c.get('resolved') or []) or '-'
        lines.append(f"- step {c.get('step')}: {c.get('note', '')}  [{resolved}]")
    text = '\n'.join(lines)
    return text[:_CARRY_MAX_CHARS]


def render(plan: dict, step_n: int, carry: Optional[List[dict]] = None) -> str:
    steps = plan.get('steps', [])
    step = next((s for s in steps if s.get('n') == step_n), None)
    if step is None:
        raise ValueError(f'plan {plan.get("task_id")!r} has no step n={step_n}')

    locate = step.get('locate_with') or []
    locate_block = ('\n'.join(f'- {loc}' for loc in locate)
                    if locate else '(none given -- use graph_search first)')

    acc = step.get('acceptance_check') or {}
    acc_line = (f"{acc.get('type')}: `{acc.get('cmd')}`  (expect {acc.get('expect', 'exit_zero')})"
               if acc.get('cmd') else '(none -- inspect-only step)')

    parts = [
        f"PLAN STEP {step_n} of {len(steps)}  --  task {plan.get('task_id')}",
        '',
        f"GOAL: {step.get('goal')}",
        f"TARGET FILE: {step.get('target_file')}  (operation: {step.get('operation')})",
        f"TOOL: {step.get('tool')}",
    ]
    if step.get('max_edit_lines'):
        parts.append(f"MAX EDIT LINES: {step['max_edit_lines']}")
    parts += [
        '',
        'LOCATE WITH (resolve these first, do not guess line numbers):',
        locate_block,
        '',
        f"EDIT SPEC: {step.get('edit_spec') or '(n/a -- inspect only)'}",
        '',
        f"ACCEPTANCE CHECK for this step: {acc_line}",
        '',
        'CONTEXT CARRIED FROM PRIOR STEPS:',
        _format_carry(carry or []),
        '',
        'Do only this step. When the acceptance check above would pass, output '
        '"STEP DONE" and stop.',
    ]
    return '\n'.join(parts)


def load_carry(path: str) -> List[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding='utf-8'))


def append_carry(path: str, step_n: int, resolved: List[str], note: str) -> None:
    p = Path(path)
    carry = load_carry(path)
    carry.append({'step': step_n, 'resolved': resolved, 'note': note})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(carry, indent=2), encoding='utf-8')


if __name__ == '__main__':
    import argparse
    import sys

    from eval.planner import load_plan

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--plan', required=True, help='task_id (loads eval/plans/<id>.json)')
    ap.add_argument('--step', type=int, required=True)
    ap.add_argument('--carry', help='path to a carry.json file')
    args = ap.parse_args()

    plan = load_plan(args.plan)
    carry = load_carry(args.carry) if args.carry else []
    print(render(plan, args.step, carry))
    sys.exit(0)
