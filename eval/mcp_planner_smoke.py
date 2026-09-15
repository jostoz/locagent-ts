"""Run one read-only Qwen planning session with LocAgent MCP enabled."""
from __future__ import annotations
import json
from pathlib import Path
from eval.cline_runner import run_once
from eval.score import _load_jsonl

HERE = Path(__file__).resolve().parent
def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--image', default='locagent-cline:stage1-v3')
    ap.add_argument('--output', type=Path, default=HERE / 'results' / 'mcp-planner-smoke-q3')
    ap.add_argument('--tasks', default='q1,q2,q3,q4,q5,q6,q7,q8,q9,q10,q11,qC1')
    ap.add_argument('--skip-existing', action='store_true')
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records = {r['id']: r for r in _load_jsonl(str(HERE / 'miro_clone_localization.jsonl'))}
    system = (HERE / 'prompts' / 'planner_system.txt').read_text(encoding='utf-8')
    summaries = []
    for task_id in args.tasks.split(','):
        if args.skip_existing and (args.output / f'{task_id}.plan.json').exists():
            continue
        record = records[task_id]
        prompt = system + ('\n\nYou also have read-only LocAgent MCP access. Use graph_search/graph_get to verify the live symbols before planning. '
                           'Do not edit files. Return only one valid JSON plan object matching the schema above.')
        prompt += f"\n\nTASK ID: {task_id}\nTASK: {record['prompt']}"
        if task_id == 'qC1':
            prompt += (
                '\nThis case can trigger excessive exploration. Make at most 4 MCP tool calls. '
                'The discriminator is the optional StickyNote.color field: verify its definition and '
                'the direct checks in useStickyNotes.ts and Board.tsx, then emit the JSON immediately. '
                'Do not search for an exhaustive list of every incidental color reference.'
            )
        result = run_once(task_id=task_id, condition='planner-mcp', step=0, prompt=prompt,
                          repo=str(args.repo), run_path=str(args.output / f'{task_id}.jsonl'),
                          provider='openai-compatible', model='qwen3.8-27b',
                          system=None, timeout_s=180, api_key='lm-studio-local',
                          sandbox_image=args.image)
        if result.flake == 'ok' and not result.transcript.final_text.strip():
            result = run_once(task_id=task_id, condition='planner-mcp-retry', step=0,
                              prompt=prompt + '\nReturn the JSON immediately with no reasoning text.',
                              repo=str(args.repo), run_path=str(args.output / f'{task_id}-retry.jsonl'),
                              provider='openai-compatible', model='qwen3.8-27b',
                              system=None, timeout_s=180, api_key='lm-studio-local',
                              sandbox_image=args.image)
        summary = {'id': task_id, 'exit': result.exit_code, 'flake': result.flake,
                   'wall_s': result.wall_s, 'graph_calls': result.transcript.n_graph}
        if result.flake == 'ok':
            from eval.planner import _extract_json, mark_big_files, validate_plan
            try:
                plan = _extract_json(result.transcript.final_text)
            except (ValueError, json.JSONDecodeError) as exc:
                summary['parse_error'] = str(exc)[:300]
                retry = run_once(task_id=task_id, condition='planner-mcp-parse-retry', step=0,
                                 prompt=prompt + '\nReturn one complete JSON object immediately; no XML wrapper or prose.',
                                 repo=str(args.repo), run_path=str(args.output / f'{task_id}-parse-retry.jsonl'),
                                 provider='openai-compatible', model='qwen3.8-27b',
                                 system=None, timeout_s=180, api_key='lm-studio-local',
                                 sandbox_image=args.image)
                summary['retry_flake'] = retry.flake
                summary['retry_graph_calls'] = retry.transcript.n_graph
                if retry.flake != 'ok':
                    summaries.append(summary)
                    (args.output / 'progress.json').write_text(json.dumps(summaries, indent=2), encoding='utf-8')
                    print(json.dumps(summary))
                    continue
                try:
                    plan = _extract_json(retry.transcript.final_text)
                except (ValueError, json.JSONDecodeError) as retry_exc:
                    summary['retry_parse_error'] = str(retry_exc)[:300]
                    summaries.append(summary)
                    (args.output / 'progress.json').write_text(json.dumps(summaries, indent=2), encoding='utf-8')
                    print(json.dumps(summary))
                    continue
            plan['planner_model'] = 'qwen3.8-27b-mcp'
            plan.setdefault('task_id', task_id)
            plan.setdefault('task', record['prompt'])
            plan = mark_big_files(plan, {'src/board/Board.tsx', 'src/board/toolbars.tsx',
                                         'src/board/useStickyNotes.ts', 'src/board/Board.test.tsx'})
            summary['plan_valid'] = not validate_plan(plan)
            (args.output / f'{task_id}.plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
        summaries.append(summary)
        (args.output / 'progress.json').write_text(json.dumps(summaries, indent=2), encoding='utf-8')
        print(json.dumps(summary))
    return 0 if all(s['flake'] == 'ok' and s.get('plan_valid') for s in summaries) else 1

if __name__ == '__main__':
    raise SystemExit(main())
