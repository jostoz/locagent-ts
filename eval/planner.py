"""Stage-1 planner: DeepSeek `deepseek-reasoner` decomposes a task into steps
small enough for the 9b executor to run one at a time, each with its own
runnable acceptance check. See ``eval/prompts/planner_system.txt`` for the
full contract and ``eval/README.md`` for the pipeline this plugs into.

    from eval.planner import plan_task, validate_plan
    plan = plan_task("e6", TASK_TEXT)          # calls DeepSeek, caches to eval/plans/e6.json
    problems = validate_plan(plan)              # [] if clean

The planner sees the task text plus a cached ``graph_map`` digest
(``eval/fixtures/miro_map.txt``) -- no live code, no ground truth (decision D5).
Key comes from ``DEEPSEEK_API_KEY`` in the environment; it is never read from
``~/.cline/data/settings/providers.json``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List, Optional

_HERE = Path(__file__).resolve().parent
PROMPT_PATH = _HERE / 'prompts' / 'planner_system.txt'
MAP_PATH = _HERE / 'fixtures' / 'miro_map.txt'
PLANS_DIR = _HERE / 'plans'
DEEPSEEK_BASE_URL = 'https://api.deepseek.com'
DEEPSEEK_MODEL = 'deepseek-reasoner'

_POSIX_PATH_RE = re.compile(r'^[A-Za-z0-9_.\-/]+$')
_BIG_FILE_LINES = 500
_MAX_EDIT_LINES_ON_BIG_FILE = 10
_VAGUE_LOCATE_RE = re.compile(r'(?i)around line \d+|somewhere (in|near)|approximately')


# --------------------------------------------------------------------- schema
def validate_plan(plan: dict) -> List[str]:
    """Return a list of problems; empty == the plan is executable as-is.
    Mirrors the rules in ``eval/prompts/planner_system.txt``."""
    problems: List[str] = []

    for key in ('task_id', 'task', 'steps', 'final_acceptance'):
        if key not in plan:
            problems.append(f'missing top-level key "{key}"')
    if problems:
        return problems

    steps = plan['steps']
    if not isinstance(steps, list) or not steps:
        return ['"steps" must be a non-empty list']

    ns = [s.get('n') for s in steps]
    if ns != list(range(1, len(steps) + 1)):
        problems.append(f'steps[].n must be contiguous 1..N, got {ns}')

    if len(steps) > 6:
        problems.append(f'plan_smell: {len(steps)} steps -- keep it short (<=6), '
                        f'more steps means more fresh-context handoffs')

    for s in steps:
        tag = f'step {s.get("n", "?")}'
        for key in ('goal', 'target_file', 'operation', 'tool', 'acceptance_check'):
            if key not in s:
                problems.append(f'{tag}: missing "{key}"')
        op = s.get('operation')
        if op not in ('inspect', 'create', 'edit'):
            problems.append(f'{tag}: operation must be inspect|create|edit, got {op!r}')

        tf = s.get('target_file', '')
        if not isinstance(tf, str) or not tf or not _POSIX_PATH_RE.match(tf) or '\\' in tf:
            problems.append(f'{tag}: target_file must be one relative POSIX path, got {tf!r}')
        elif tf.startswith('/') or tf.startswith('../'):
            problems.append(f'{tag}: target_file must be repo-relative, got {tf!r}')

        tool = s.get('tool')
        if op == 'create' and tool != 'editor':
            problems.append(f'{tag}: operation "create" must use tool "editor", got {tool!r}')

        edit_spec = s.get('edit_spec', '') or ''
        if op == 'edit':
            if _VAGUE_LOCATE_RE.search(edit_spec):
                problems.append(f'{tag}: edit_spec is a vague locator ("around line N" style) '
                                f'-- anchor to a quoted line or named symbol instead')
            mel = s.get('max_edit_lines')
            # target_file size is unknown to the planner at validate time (it
            # only sees the module map) -- the caller passes big_files to check
            # against when it knows; here we only enforce presence when the
            # step itself claims a large-file edit via the "big" hint.
            if s.get('_target_is_big') and (mel is None or mel > _MAX_EDIT_LINES_ON_BIG_FILE):
                problems.append(f'{tag}: edit on a >500-line file needs max_edit_lines <= '
                                f'{_MAX_EDIT_LINES_ON_BIG_FILE}, got {mel!r}')

        ac = s.get('acceptance_check') or {}
        if op != 'inspect' and (not ac.get('cmd')) and ac.get('type') != 'none':
            problems.append(f'{tag}: acceptance_check.cmd is required (type "none" only for inspect)')

        loc = s.get('locate_with')
        if loc is not None and not isinstance(loc, list):
            problems.append(f'{tag}: locate_with must be a list of strings')

    fa = plan.get('final_acceptance') or {}
    if 'cmd' not in fa or 'type' not in fa:
        problems.append('final_acceptance must have "type" and "cmd" (cmd may be "" only for type "none")')

    return problems


def mark_big_files(plan: dict, big_files: set) -> dict:
    """Annotate steps whose target_file is in *big_files* (>500 lines) so
    validate_plan can enforce max_edit_lines on them. Mutates and returns plan."""
    for s in plan.get('steps', []):
        if s.get('target_file') in big_files:
            s['_target_is_big'] = True
    return plan


# ------------------------------------------------------------------- planning
def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    return json.loads(text)


def _client():
    try:
        import openai  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            'openai package not installed -- pip install -r requirements-eval.txt') from e
    key = os.environ.get('DEEPSEEK_API_KEY')
    if not key:
        raise RuntimeError(
            'DEEPSEEK_API_KEY not set. Never read it from '
            '~/.cline/data/settings/providers.json -- export it in the environment.')
    return openai.OpenAI(base_url=DEEPSEEK_BASE_URL, api_key=key)


def plan_task(
    task_id: str,
    task_text: str,
    *,
    refresh: bool = False,
    big_files: Optional[set] = None,
    model: str = DEEPSEEK_MODEL,
) -> dict:
    """Call DeepSeek to plan *task_text*; cache to ``eval/plans/<task_id>.json``.
    Returns the cached plan unless *refresh* is set."""
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = PLANS_DIR / f'{task_id}.json'
    if cache_path.exists() and not refresh:
        plan = json.loads(cache_path.read_text(encoding='utf-8'))
        return mark_big_files(plan, big_files or set())

    system = PROMPT_PATH.read_text(encoding='utf-8')
    repo_map = MAP_PATH.read_text(encoding='utf-8') if MAP_PATH.exists() else '(no map cached)'
    user = (
        f'task_id: {task_id}\n\n'
        f'TASK:\n{task_text}\n\n'
        f'REPO MODULE MAP (miro-clone, digest only -- no source):\n{repo_map}'
    )

    client = _client()
    resp = client.chat.completions.create(
        model=model,
        messages=[{'role': 'system', 'content': system},
                  {'role': 'user', 'content': user}],
        response_format={'type': 'json_object'},
    )
    raw = resp.choices[0].message.content
    plan = _extract_json(raw)
    plan.setdefault('task_id', task_id)
    plan.setdefault('planner_model', model)
    plan.setdefault('task', task_text)

    cache_path.write_text(json.dumps(plan, indent=2), encoding='utf-8')
    return mark_big_files(plan, big_files or set())


def load_plan(task_id: str) -> dict:
    path = PLANS_DIR / f'{task_id}.json'
    return json.loads(path.read_text(encoding='utf-8'))


# ------------------------------------------------------------------------- cli
def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--task', required=True, help='task_id (looked up in the corpus) or --prompt')
    ap.add_argument('--prompt', help='task text; overrides corpus lookup')
    ap.add_argument('--refresh-plans', action='store_true')
    ap.add_argument('--dry-run', action='store_true', help='validate only, do not print the plan body')
    ap.add_argument('--big-files', default='src/board/Board.tsx,src/board/toolbars.tsx,'
                    'src/board/useStickyNotes.ts,src/board/Board.test.tsx',
                    help='comma-separated repo-relative paths treated as >500 lines')
    args = ap.parse_args(argv)

    task_text = args.prompt
    if task_text is None:
        from eval.score import _load_jsonl
        records = {r['id']: r for r in _load_jsonl(str(_HERE / 'miro_clone_localization.jsonl'))}
        if args.task not in records:
            ap.error(f'{args.task!r} not in the corpus; pass --prompt for an ad hoc task')
        task_text = records[args.task]['prompt']

    big = set(f.strip() for f in args.big_files.split(',') if f.strip())
    plan = plan_task(args.task, task_text, refresh=args.refresh_plans, big_files=big)
    problems = validate_plan(plan)

    if problems:
        print(f'{len(problems)} problem(s):')
        for p in problems:
            print(f'  - {p}')
    else:
        print(f'plan for {args.task}: {len(plan["steps"])} step(s), valid.')
    if not args.dry_run:
        print(json.dumps(plan, indent=2))
    return 1 if problems else 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
