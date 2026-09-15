"""Reproducible Stage-1 matrix: local executor solo vs DeepSeek-planned steps.

The runner deliberately plans before starting Cline and removes
``DEEPSEEK_API_KEY`` from every executor subprocess.  Run from this checkout:

  python -m eval.run_matrix --repo C:/Users/joz/Documents/miro-clone --condition both

Edit records run in detached worktrees below the run directory.  They are never
run against the supplied checkout.  Results, transcripts, plans, predictions,
and a compact summary are written under ``eval/results/<run-id>/``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from eval.cline_runner import run_once
from eval.guard import analyze
from eval.metrics import _load_jsonl, evaluate, to_file_level
from eval.planner import mark_big_files, plan_task, validate_plan
from eval.render_step import render
from eval.score import score_answer, validate_corpus

HERE = Path(__file__).resolve().parent
EXECUTOR_SYSTEM = (HERE / 'prompts' / 'executor_system.txt').read_text(encoding='utf-8')
DEFAULT_MODEL = 'qwen3.8-27b'
DEFAULT_PROVIDER = 'openai-compatible'
LOCAL_API_MARKER = 'lm-studio-local'
ENTITY_RE = re.compile(r'(?<![A-Za-z0-9_.-/])([\w./-]+\.(?:ts|tsx):[A-Za-z_$][\w.$]*)')


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _assert_pin(repo: Path, records: list[dict]) -> str:
    pins = {r['pinned_at'] for r in records}
    if len(pins) != 1:
        raise RuntimeError(f'corpus must have one pinned revision, got {sorted(pins)}')
    pin = next(iter(pins))
    try:
        _git(repo, 'rev-parse', '--verify', f'{pin}^{{commit}}')
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f'corpus pin {pin} is unavailable in {repo}; fetch it or re-pin first') from exc
    return pin


def _worktree(source: Path, destination: Path, revision: str) -> Path:
    if destination.exists():
        raise RuntimeError(f'worktree path already exists: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', '-C', str(source), 'worktree', 'add', '--detach', str(destination), revision], check=True)
    return destination


def _acceptance(repo: Path, commands: Iterable[str], tooling_repo: Path) -> tuple[bool, list[dict]]:
    """Run post-executor checks with a transient junction to pinned tooling.

    Worktrees intentionally contain no node_modules and the container never sees
    the source checkout. The evaluator may reuse its installed TypeScript
    packages after Cline exits; the junction is removed before returning.
    """
    source_modules = tooling_repo / 'node_modules'
    worktree_modules = repo / 'node_modules'
    if not source_modules.is_dir():
        return False, [{'cmd': '<tooling>', 'exit': 1,
                        'tail': f'missing evaluator node_modules: {source_modules}'}]
    if os.path.lexists(worktree_modules):
        # A sandboxed executor may leave a partial dependency tree behind
        # after an interrupted package install. It is disposable evaluator
        # state, so remove it before installing the read-only tooling junction.
        if worktree_modules.is_dir() and not worktree_modules.is_symlink():
            shutil.rmtree(worktree_modules, ignore_errors=True)
        else:
            worktree_modules.unlink(missing_ok=True)
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(worktree_modules), str(source_modules)],
                   check=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
    outcomes = []
    env = dict(os.environ)
    env['npm_config_offline'] = 'true'
    try:
        for command in commands:
        # Corpus grep checks came from a POSIX scratch harness.  Execute their
        # intended regex semantics directly so Windows does not turn a correct
        # patch into a false negative merely because grep is absent.
            exists = re.fullmatch(r'test\s+-f\s+(.+)', command)
            if exists:
                relative = exists.group(1).strip()
                ok = (repo / relative).is_file()
                outcomes.append({'cmd': command, 'exit': 0 if ok else 1,
                                 'tail': 'file exists' if ok else 'file does not exist'})
                continue
            grep = re.fullmatch(r'''grep\s+-nE?\s+(?:"([^"]+)"|'([^']+)')\s+(.+)''', command)
            if grep:
                pattern = grep.group(1) or grep.group(2)
                relative = grep.group(3).strip()
            # Early JSONL rows over-escaped a literal question mark. POSIX
            # grep receives one backslash after shell decoding.
                pattern = pattern.replace('\\\\', '\\')
                try:
                    text = (repo / relative).read_text(encoding='utf-8')
                    ok = re.search(pattern, text, re.MULTILINE) is not None
                    outcomes.append({'cmd': command, 'exit': 0 if ok else 1,
                                     'tail': 'matched' if ok else 'pattern did not match'})
                except (OSError, re.error) as exc:
                    outcomes.append({'cmd': command, 'exit': 1, 'tail': str(exc)})
                continue
            if command.startswith('npx '):
                command = command.replace('npx ', 'npx --no-install ', 1)
            result = subprocess.run(command, cwd=repo, shell=True, text=True, env=env,
                                    encoding='utf-8', errors='replace',
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=300)
            outcomes.append({'cmd': command, 'exit': result.returncode, 'tail': result.stdout[-1200:]})
    finally:
        os.rmdir(worktree_modules)
    return all(row['exit'] == 0 for row in outcomes), outcomes


def _entities_from_run(result) -> list[str]:
    ordered: list[str] = []
    def add(value: object) -> None:
        if isinstance(value, str):
            for entity in ENTITY_RE.findall(value):
                if entity not in ordered:
                    ordered.append(entity)
        elif isinstance(value, dict):
            for v in value.values():
                add(v)
        elif isinstance(value, list):
            for v in value:
                add(v)
    for call in result.transcript.tool_calls:
        add(call.input)
        for output in call.outputs:
            add(output.get('result'))
    add(result.transcript.final_text)
    return ordered


def _run_executor(*, record: dict, condition: str, repo: Path, run_dir: Path,
                  prompt: str, step: Optional[int], model: str, timeout: int,
                  sandbox_image: Optional[str], disable_locagent: bool = False):
    # Never pass DeepSeek credentials to Cline.  The MCP needs only these paths.
    previous = os.environ.pop('DEEPSEEK_API_KEY', None)
    os.environ['LOCAGENT_REPO'] = str(repo)
    os.environ['LOCAGENT_CACHE_DIR'] = str(repo.parent / '.locagent-cache' / repo.name)
    try:
        return run_once(
            task_id=record['id'], condition=condition, step=step, prompt=prompt,
            repo=str(repo), run_path=str(run_dir / f'{condition}-{record["id"]}-step{step or 0}.jsonl'),
            provider=DEFAULT_PROVIDER, model=model, system=EXECUTOR_SYSTEM,
            timeout_s=timeout, api_key=LOCAL_API_MARKER, sandbox_image=sandbox_image,
            disable_locagent=disable_locagent)
    finally:
        if previous is not None:
            os.environ['DEEPSEEK_API_KEY'] = previous


def _run_record(record: dict, *, condition: str, source_repo: Path, run_dir: Path,
                model: str, timeout: int, source_status: str,
                sandbox_image: Optional[str], plans_dir: Optional[Path] = None,
                disable_locagent: bool = False) -> dict:
    is_edit = record['category'].startswith('edit/')
    if is_edit and not sandbox_image:
        raise RuntimeError('edit tasks require --sandbox-image; Cline cwd/worktree flags are not a filesystem boundary')
    # Never let a task see or alter the caller's checkout.  A user may keep
    # working on a newer branch; every condition instead starts from the corpus
    # revision, so scores remain comparable to its recorded ground truth.
    repo = _worktree(source_repo, run_dir / 'worktrees' / f'{condition}-{record["id"]}', record['pinned_at'])
    baseline = _git(repo, 'rev-parse', 'HEAD')
    runs = []
    plan = None
    if condition == 'solo':
        prompts = [(None, record['prompt'])]
    else:
        plan = plan_task(record['id'], record['prompt'], big_files=set(), plans_dir=plans_dir)
        problems = validate_plan(plan)
        if problems:
            return {'id': record['id'], 'condition': condition, 'status': 'invalid_plan', 'problems': problems}
        prompts, carry = [], []
        for step in plan['steps']:
            prompts.append((step['n'], render(plan, step['n'], carry)))
            carry.append({'step': step['n'], 'resolved': [], 'note': step['goal']})

    for step, prompt in prompts:
        result = _run_executor(record=record, condition=condition, repo=repo, run_dir=run_dir,
                               prompt=prompt, step=step, model=model, timeout=timeout,
                               sandbox_image=sandbox_image, disable_locagent=disable_locagent)
        if _git(source_repo, 'status', '--porcelain') != source_status:
            raise RuntimeError(
                'executor changed the source checkout outside its worktree; '
                'aborting to preserve user changes')
        guard = analyze(result.transcript, repo=str(repo), target_file=(plan or {}).get('target_file'),
                        expect_edit=is_edit, baseline_ref=baseline,
                        shell='linux' if sandbox_image else 'windows')
        runs.append({'step': step, 'exit': result.exit_code, 'flake': result.flake,
                     'wall_s': result.wall_s, 'graph_calls': result.transcript.n_graph,
                     'guard': guard.as_dict(), 'final': result.transcript.final_text,
                     'entities': _entities_from_run(result),
                     'stderr_tail': result.stderr_tail})
        fatal = {'shell_write', 'tmp_file', 'off_target_edit', 'whole_file_rewrite'}
        operation = None
        if plan is not None and step is not None:
            operation = next((item.get('operation') for item in plan['steps'] if item['n'] == step), None)
        if is_edit and (condition == 'solo' or operation in {'create', 'edit'}):
            fatal.add('no_progress')
        if result.flake != 'ok' or any(flag in fatal for flag in guard.flags):
            break

    final = '\n'.join(r['final'] for r in runs)
    score = None
    if record.get('answer_tokens'):
        score = score_answer(final, record['answer_tokens'])
    accepted, acceptance = (None, [])
    if is_edit and all(r['flake'] == 'ok' for r in runs):
        accepted, acceptance = _acceptance(repo, record['acceptance'], source_repo)
    entities = [e for run in runs for e in run['entities']]
    entities = list(dict.fromkeys(entities))
    return {'id': record['id'], 'condition': condition, 'status': 'complete',
            'repo': str(repo), 'is_edit': is_edit, 'runs': runs,
            'answer_score': score[0] if score else None,
            'answer_correct': score[1] if score else None,
            'acceptance_ok': accepted, 'acceptance': acceptance,
            'ranked_entities': entities, 'ranked_files': to_file_level(entities)}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', required=True, type=Path)
    ap.add_argument('--condition', choices=('solo', 'planned', 'both'), default='both')
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--timeout', type=int, default=300)
    ap.add_argument('--tasks', help='comma-separated task ids; default: all 18')
    ap.add_argument('--run-id', default=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    ap.add_argument('--sandbox-image', help='Docker image for filesystem-isolated executors; required for edit tasks')
    ap.add_argument('--plans-dir', type=Path, help='plan cache directory for planned condition')
    ap.add_argument('--no-locagent', action='store_true', help='disable LocAgent MCP in executor container')
    args = ap.parse_args(argv)
    secret_file = HERE / '.env'
    if secret_file.exists():
        raise SystemExit(
            'refusing to start executors while eval/.env exists: Cline has host-level '
            'read tools and can expose the DeepSeek credential. Generate/cache plans first, '
            'then remove eval/.env before running the matrix.')
    problems = validate_corpus(str(HERE / 'miro_clone_localization.jsonl'))
    if problems:
        raise SystemExit('\n'.join(problems))
    records = _load_jsonl(str(HERE / 'miro_clone_localization.jsonl'))
    if args.tasks:
        wanted = set(args.tasks.split(','))
        records = [r for r in records if r['id'] in wanted]
        missing = wanted - {r['id'] for r in records}
        if missing:
            raise SystemExit(f'unknown task ids: {sorted(missing)}')
    _assert_pin(args.repo, records)
    source_status = _git(args.repo, 'status', '--porcelain')
    run_dir = HERE / 'results' / args.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    conditions = ('solo', 'planned') if args.condition == 'both' else (args.condition,)
    rows = []
    for condition in conditions:
        for record in records:
            row = _run_record(record, condition=condition, source_repo=args.repo, run_dir=run_dir,
                          model=args.model, timeout=args.timeout, source_status=source_status,
                          sandbox_image=args.sandbox_image, plans_dir=args.plans_dir,
                          disable_locagent=args.no_locagent)
            rows.append(row)
            (run_dir / f'{condition}-{record["id"]}.summary.json').write_text(json.dumps(row, indent=2), encoding='utf-8')
    for condition in conditions:
        pred = {r['id']: r for r in rows if r['condition'] == condition}
        (run_dir / f'{condition}.pred.jsonl').write_text(
            ''.join(json.dumps({'id': r['id'], 'ranked_entities': r.get('ranked_entities', []),
                                'ranked_files': r.get('ranked_files', [])}) + '\n' for r in pred.values()), encoding='utf-8')
        localize = [r for r in records if not r['category'].startswith('edit/')]
        metrics = evaluate(localize, pred)
        edits = [r for r in pred.values() if r.get('is_edit')]
        metrics['edits'] = {
            'n': len(edits),
            'acceptance_passed': sum(r.get('acceptance_ok') is True for r in edits),
            'guard_clean': sum(not any(step['guard']['flags'] for step in r.get('runs', [])) for r in edits),
        }
        (run_dir / f'{condition}.metrics.json').write_text(json.dumps(metrics, indent=2), encoding='utf-8')
    with (run_dir / 'summary.csv').open('w', newline='', encoding='utf-8') as fh:
        fields = ['id', 'condition', 'status', 'answer_score', 'answer_correct', 'acceptance_ok']
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fields})
    print(run_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
