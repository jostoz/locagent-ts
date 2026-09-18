"""Run one localization case through Cline without LocAgent MCP."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from eval.cline_runner import run_once
from eval.score import _load_jsonl, score_answer

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', required=True)
    ap.add_argument('--task', required=True)
    ap.add_argument('--model', required=True)
    ap.add_argument('--base-url', required=True)
    ap.add_argument('--provider', default='openai-compatible')
    ap.add_argument('--api-key-env', required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--thinking', default='none', choices=['none', 'low', 'medium', 'high', 'xhigh'])
    ap.add_argument('--image', default='locagent-cline:stage1-v3')
    args = ap.parse_args()

    key = os.environ.get(args.api_key_env)
    if not key:
        raise RuntimeError(f'{args.api_key_env} is not set')
    records = {r['id']: r for r in _load_jsonl(str(HERE / 'miro_clone_localization.jsonl'))}
    record = records[args.task]
    args.output.mkdir(parents=True, exist_ok=True)
    result = run_once(
        task_id=args.task,
        condition='provider-no-mcp',
        prompt=record['prompt'],
        repo=args.repo,
        run_path=str(args.output / 'transcript.jsonl'),
        provider=args.provider,
        model=args.model,
        system=(HERE / 'prompts' / 'executor_system.txt').read_text(encoding='utf-8'),
        thinking=args.thinking,
        timeout_s=240,
        expect_mcp=False,
        api_key=key,
        base_url=args.base_url,
        sandbox_image=args.image,
        disable_locagent=True,
    )
    score, correct = score_answer(result.transcript.final_text, record['answer_tokens'])
    summary = {
        'task': args.task,
        'model': args.model,
        'base_url_host': args.base_url.split('/')[2],
        'thinking': args.thinking,
        'exit': result.exit_code,
        'flake': result.flake,
        'wall_s': result.wall_s,
        'iterations': result.transcript.iterations,
        'native_calls': result.transcript.n_native,
        'graph_calls': result.transcript.n_graph,
        'input_tokens': result.transcript.usage.get('inputTokens', 0),
        'output_tokens': result.transcript.usage.get('outputTokens', 0),
        'answer_score': score,
        'answer_correct': correct,
        'final': result.transcript.final_text,
    }
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in summary.items() if k != 'final'}))
    return 0 if result.flake == 'ok' and correct and result.transcript.n_graph == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
