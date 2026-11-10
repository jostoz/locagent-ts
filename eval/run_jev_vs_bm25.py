"""TASK-003: Gestado+BM25+AST vs Gestado+Jev+AST, held-out path-free EDIT case.

Runs the single held-out edit record in ``eval/heldout_edit_case.jsonl`` (never
part of ``eval/miro_clone_localization.jsonl``) through two arms that are
identical except for the localization mechanism:

  Arm A  Gestado + graph_search's BM25 index (``dependency_graph/ts_bm25.py``) + AST
  Arm B  Gestado + jegrep (``locagent_mcp.py:_jegrep_hits`` mechanism) canonicalized
         against AST-entity ids (``eval/jev_canonicalize.py``) + AST

Both arms, per record:
  1. localize in a subprocess (``eval/localize_helper.py``) against a fresh,
     disposable ``git worktree`` of miro-clone pinned at the record's
     ``pinned_at`` revision -- never the live checkout;
  2. hand the executor (Cline CLI, ``qwen3.8-27b`` at 32768 ctx via LM Studio)
     ONLY the canonical entity package as plain text in the prompt -- the
     LocAgent MCP is disabled in the sandbox (``disable_locagent=True``), so
     the model cannot call graph_search/graph_semantic_search itself and can
     never see jegrep's raw ranking or snippets;
  3. run inside ``--sandbox-image`` (container filesystem boundary, same
     mechanism as ``eval/run_matrix.py`` / ``eval/isolated_env.py``);
  4. validate with the fixture's own ``acceptance`` shell commands
     (``eval.run_matrix._acceptance``);
  5. thread inter-step state through GESTADO's deterministic merge
     (``eval/gestado_state.py``) instead of an ad-hoc carry dict.

Usage::

  python -m eval.run_jev_vs_bm25 --repo C:/Users/joz/Documents/miro-clone \\
      --sandbox-image locagent-cline:stage1-v4 --model qwen3.8-27b

Writes ``eval/results/<run-id>-jev-vs-bm25/`` with per-arm result JSON,
per-arm prediction JSONL (for ``python -m eval.metrics``), and a combined
``report.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from eval.cline_runner import run_once
from eval.gestado_state import advance, initial_sigma
from eval.guard import analyze
from eval.metrics import evaluate, to_file_level
from eval.run_matrix import _acceptance, _git, _worktree
from eval.score import validate_corpus

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
FIXTURE = HERE / 'heldout_edit_case.jsonl'
DEFAULT_MODEL = 'qwen3.8-27b'
DEFAULT_PROVIDER = 'openai-compatible'
LOCAL_API_MARKER = 'lm-studio-local'
DEFAULT_SANDBOX_IMAGE = 'locagent-cline:stage1-v4'
DEFAULT_CONTEXT = 65536
# temperature=0 + a hard reasoning cap, not a mere --thinking request, are what
# forge-platform/triad-integration already validated for this exact model
# (qwen3.8-27b) before this session: without them the model shows real
# sampling variance under tool-calling pressure -- the identical canonical
# package produced a clean 5-call success once and a 132-call identical-chunk
# loop another time in this session's own runs.
DEFAULT_TEMPERATURE = 0.0
DEFAULT_REASONING_MAX_TOKENS = 8000
DEFAULT_MAX_RESULTS = 15

EXECUTOR_SYSTEM = """You are the EXECUTOR in a controlled localization-mechanism \
experiment. You run in a Linux container whose only writable path is your \
current working directory (a disposable git worktree, no node_modules, no \
network). A code-search tool is not offered to you in this run.

You are given, in the user message, a CANONICAL LOCALIZATION PACKAGE: a list \
of entity ids (file:Name) with each entity's absolute file path (already \
rooted at /workspace -- pass it to your read/editor tools exactly as given, \
never stripped, relativized, or with a different prefix guessed), its line \
range (L<start>-<end>), and a one-line head. The set of files named in the \
package is the ONLY set of files you may read or edit in this run -- never \
open a file that is not \
named in the package, even if a comment or import inside a package file \
mentions another file; that other file is out of scope for this task \
regardless of what it contains. The change you need is inside one of the \
package's file/range pairs -- if the first chunk you read of an entity's \
range does not contain it, read the entity's remaining chunks (see below) \
before considering anything else.

That line range is exact -- it already identifies the function or class to \
edit. Your FIRST read of a file in the package MUST pass that same \
L<start>-<end> range, not the whole file. Never read more than 150 lines in \
a single call -- when the package's range is longer than 150 lines, read it \
in consecutive, non-overlapping 150-line chunks in order (e.g. first \
L316-465, then L466-626): reading the NEXT chunk of an entity you have not \
finished reading yet is required, not a re-read, and is different from \
re-reading a chunk you already have. Once you have read every chunk of an \
entity's range, never issue any of those same reads again, in this file or \
any other -- if you need more context around a range you fully covered, \
reason from what you already read instead of reading any of it again. Use \
ONLY the package plus your editor/read tools (read an explicit line range, \
then edit) to find where to make the requested change. Never rewrite a \
whole file; edit the specific region. Never use shell redirection, \
heredocs, tee, or echo to write source -- use only the editor tool.

NEVER call run_commands / execute a shell command for ANY reason -- not to \
install dependencies, not to run npm/tsc/git, not to verify your edit. There \
is no package registry reachable and the root filesystem is read-only; any \
shell command will fail or hang. If you want to check your own edit, re-read \
the file with the read tool instead.

Do not ask the user to apply anything manually; make the edit yourself. \
Do not create temporary, backup, or partial files. Finish with exactly \
`STEP DONE`, or `STEP BLOCKED: <one line why>`, then stop -- as soon as the \
editor tool call that makes the change has succeeded."""


def _render_package(entities: list[dict]) -> str:
    if not entities:
        return '(package is empty -- localization produced no usable entity)'
    lines = ['CANONICAL LOCALIZATION PACKAGE (entity id | absolute file path | lines | head):']
    for e in entities:
        abs_path = f"/workspace/{e['file']}"
        lines.append(f"- {e['id']}  |  {abs_path}  |  L{e.get('start_line')}-{e.get('end_line')}"
                     + (f"  |  {e['head']}" if e.get('head') else ''))
    return '\n'.join(lines)


def _build_prompt(record: dict, entities: list[dict]) -> str:
    return f"{record['prompt']}\n\n{_render_package(entities)}"


def _localize(*, worktree: Path, arm: str, query: str, max_results: int) -> Dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, '-m', 'eval.localize_helper',
         '--repo', str(worktree), '--arm', arm, '--query', query,
         '--max-results', str(max_results)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f'localize_helper ({arm}) failed: {proc.stderr[-2000:]}')
    return json.loads(proc.stdout)


def run_arm(*, arm: str, source_repo: Path, record: dict, run_dir: Path,
           model: str, timeout: int, sandbox_image: str, max_results: int,
           run_id: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    sigma = initial_sigma(run_id=run_id, arm=arm, record_id=record['id'])

    worktree = _worktree(source_repo, run_dir / 'worktrees' / arm, record['pinned_at'])
    baseline = _git(worktree, 'rev-parse', 'HEAD')

    t0 = time.time()
    loc = _localize(worktree=worktree, arm=arm, query=record['prompt'], max_results=max_results)
    t_localize = round(time.time() - t0, 2)

    sigma = advance(sigma, {
        'phase': 'localized',
        'canonical_package': loc['ranked_entities'],
        'canonical_package_bytes': loc['canonical_package_bytes'],
        'guard_rejections': loc['guard_rejections'],
        'guard_rejection_count': len(loc['guard_rejections']),
        'wall_time_s': {'localize': t_localize},
    })

    prompt = _build_prompt(record, loc['entities'])
    run_path = run_dir / f'{arm}-{record["id"]}.jsonl'

    previous_deepseek = os.environ.pop('DEEPSEEK_API_KEY', None)
    t0 = time.time()
    try:
        result = run_once(
            task_id=record['id'], condition=arm, prompt=prompt, repo=str(worktree),
            run_path=str(run_path), provider=DEFAULT_PROVIDER, model=model,
            system=EXECUTOR_SYSTEM, timeout_s=timeout, api_key=LOCAL_API_MARKER,
            max_tokens=8192, temperature=DEFAULT_TEMPERATURE,
            reasoning_max_tokens=DEFAULT_REASONING_MAX_TOKENS,
            sandbox_image=sandbox_image, disable_locagent=True,
            disabled_tools=('run_commands', 'search_codebase'),
            expect_mcp=False)
    finally:
        if previous_deepseek is not None:
            os.environ['DEEPSEEK_API_KEY'] = previous_deepseek
    t_edit = round(time.time() - t0, 2)

    guard = analyze(result.transcript, repo=str(worktree), target_file=None,
                    expect_edit=True, baseline_ref=baseline, shell='linux')

    sigma = advance(sigma, {
        'phase': 'edited',
        'wall_time_s': {'edit': t_edit},
        'tokens': dict(result.transcript.usage),
    })

    accepted, acceptance = False, []
    if result.flake == 'ok':
        accepted, acceptance = _acceptance(worktree, record['acceptance'], source_repo)

    sigma = advance(sigma, {
        'phase': 'validated',
        'edit_applied': result.flake == 'ok',
        'acceptance_ok': accepted,
        'acceptance': acceptance,
    })

    ranked_entities = loc['ranked_entities']
    prediction = {'id': record['id'], 'ranked_entities': ranked_entities,
                  'ranked_files': to_file_level(ranked_entities)}

    arm_result = {
        'arm': arm,
        'record_id': record['id'],
        'worktree': str(worktree),
        'baseline_ref': baseline,
        'sandbox_image': sandbox_image,
        'model': model,
        'localization_mechanism': loc['mechanism'],
        'canonical_package': loc['entities'],
        'canonical_package_bytes': loc['canonical_package_bytes'],
        'guard_rejections': loc['guard_rejections'],
        'guard_rejection_count': len(loc['guard_rejections']),
        'edit_flake': result.flake,
        'cline_exit_code': result.exit_code,
        'edit_applied': result.flake == 'ok',
        'end_to_end_success': bool(result.flake == 'ok' and accepted),
        'acceptance_ok': accepted,
        'acceptance': acceptance,
        'wall_time_s': {'localize': t_localize, 'edit': t_edit},
        'llm_iterations': result.transcript.iterations,
        'usage': dict(result.transcript.usage),
        'guard_flags': guard.as_dict(),
        'gestado_sigma_final': sigma,
        'prediction': prediction,
    }
    return arm_result, prediction


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', required=True, type=Path, help='miro-clone source checkout (never written to)')
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--timeout', type=int, default=420)
    ap.add_argument('--sandbox-image', default=DEFAULT_SANDBOX_IMAGE)
    ap.add_argument('--max-results', type=int, default=DEFAULT_MAX_RESULTS)
    ap.add_argument('--record-id', default='h1')
    ap.add_argument('--run-id', default=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    ap.add_argument('--out-dir', type=Path, default=None)
    args = ap.parse_args(argv)

    problems = validate_corpus(str(FIXTURE))
    if problems:
        raise SystemExit('\n'.join(problems))
    with open(FIXTURE, encoding='utf-8') as fh:
        records = {json.loads(ln)['id']: json.loads(ln) for ln in fh if ln.strip()}
    if args.record_id not in records:
        raise SystemExit(f'unknown record id {args.record_id!r} in {FIXTURE}')
    record = records[args.record_id]

    localization_corpus_ids = set()
    stage1_corpus = REPO_ROOT / 'eval' / 'miro_clone_localization.jsonl'
    if stage1_corpus.exists():
        with open(stage1_corpus, encoding='utf-8') as fh:
            localization_corpus_ids = {json.loads(ln)['id'] for ln in fh if ln.strip()}
    if record['id'] in localization_corpus_ids:
        raise SystemExit(f'record {record["id"]!r} must stay OUT of {stage1_corpus.name} (held-out requirement)')

    run_dir = args.out_dir or (HERE / 'results' / f'{args.run_id}-jev-vs-bm25')
    run_dir.mkdir(parents=True, exist_ok=True)

    arm_results = {}
    predictions = {}
    for arm in ('bm25', 'jev'):
        arm_result, prediction = run_arm(
            arm=arm, source_repo=args.repo, record=record, run_dir=run_dir,
            model=args.model, timeout=args.timeout, sandbox_image=args.sandbox_image,
            max_results=args.max_results, run_id=args.run_id)
        arm_results[arm] = arm_result
        predictions[arm] = prediction
        (run_dir / f'{arm}.json').write_text(
            json.dumps(arm_result, indent=2, ensure_ascii=False), encoding='utf-8')
        (run_dir / f'{arm}.pred.jsonl').write_text(
            json.dumps(prediction, ensure_ascii=False) + '\n', encoding='utf-8')

    metrics = {arm: evaluate([record], {record['id']: predictions[arm]})
              for arm in ('bm25', 'jev')}
    (run_dir / 'metrics.json').write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False), encoding='utf-8')

    summary = {
        'run_id': args.run_id,
        'record_id': record['id'],
        'source_repo': str(args.repo),
        'pinned_at': record['pinned_at'],
        'model': args.model,
        'sandbox_image': args.sandbox_image,
        'context_window': DEFAULT_CONTEXT,
        'arms': {
            arm: {
                'end_to_end_success': arm_results[arm]['end_to_end_success'],
                'guard_rejection_count': arm_results[arm]['guard_rejection_count'],
                'canonical_package_bytes': arm_results[arm]['canonical_package_bytes'],
                'wall_time_s': arm_results[arm]['wall_time_s'],
                'usage': arm_results[arm]['usage'],
                'acceptance': arm_results[arm]['acceptance'],
                'metrics_entity': metrics[arm]['entity'],
            }
            for arm in ('bm25', 'jev')
        },
    }
    (run_dir / 'summary.json').write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')

    report = [f'# TASK-003 jev-vs-bm25 held-out edit run `{args.run_id}`', '',
             f'Record: `{record["id"]}` (held out of `eval/miro_clone_localization.jsonl`, pinned `{record["pinned_at"]}`).',
             f'Model: `{args.model}` @ {DEFAULT_CONTEXT} ctx. Sandbox image: `{args.sandbox_image}`.',
             '',
             '| arm | end-to-end success | acceptance | guard rejections | package bytes | localize s | edit s |',
             '|---|---|---|---|---|---|---|']
    for arm in ('bm25', 'jev'):
        r = arm_results[arm]
        acc_str = '; '.join(f"{a['cmd']}={'PASS' if a['exit'] == 0 else 'FAIL'}" for a in r['acceptance']) or 'n/a'
        report.append(f"| {arm} | {r['end_to_end_success']} | {acc_str} | {r['guard_rejection_count']} | "
                      f"{r['canonical_package_bytes']} | {r['wall_time_s']['localize']} | {r['wall_time_s']['edit']} |")
    report += ['', '## cross-repo evidence (contracts/project.md invariante 6)', '',
              'This repo cannot see or verify changes inside `C:/Users/joz/Documents/miro-clone` '
              '(a separate git repository) via `authority.can_modify`/`verify.sh`. The evidence that '
              'both arms actually touched that repo, in an isolated disposable worktree, and applied '
              'a real edit that was (or was not) validated lives entirely in this run directory:',
              f'- `{arm}.json` per arm records the worktree path used '
              '(`worktree`, deleted after the run but the path + baseline_ref + Cline transcript file remain '
              'as evidence it was a fresh `git worktree add --detach` off the pinned commit, never the live checkout),',
              '- the raw Cline `--json` transcript is at `<arm>-h1.jsonl` in this directory (tool calls, '
              'editor writes, exit code),',
              '- `acceptance` lists the exact shell commands from the fixture and their exit codes, run '
              'inside that worktree after the executor exited,',
              '- `gestado_sigma_final` in each `<arm>.json` is the GESTADO Sigma(t) this runner reached '
              '(phase=validated) via `gestado.stateful.merge_state`, not a free-form transcript.',
              '',
              '## GESTADO state management', '',
              'See `eval/gestado_state.py` module docstring: each arm folds `init -> localized -> edited -> '
              '-> validated` through `gestado.stateful.merge_state` (`Sigma(t+1) = Sigma(t) xor DeltaSigma(t)`), '
              'replacing the ad-hoc `carry.json` list `eval/render_step.py` builds for the Stage-1/2 planner. '
              'The final Sigma(t) dict is embedded verbatim in each `<arm>.json` under `gestado_sigma_final`.',
              '']
    (run_dir / 'report.md').write_text('\n'.join(report), encoding='utf-8')

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
