"""Generate the same Stage-1 plan contract with the local LM Studio model."""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from pathlib import Path

from eval.planner import _extract_json, mark_big_files, validate_plan
from eval.score import _load_jsonl

HERE = Path(__file__).resolve().parent
MODEL = 'qwen3.8-27b'
URL = 'http://127.0.0.1:11434/v1/chat/completions'


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--tasks', default='all')
    ap.add_argument('--output', type=Path, default=HERE / 'plans-local')
    ap.add_argument('--skip-existing', action='store_true')
    args = ap.parse_args()
    records = _load_jsonl(str(HERE / 'miro_clone_localization.jsonl'))
    wanted = {r['id'] for r in records} if args.tasks == 'all' else set(args.tasks.split(','))
    system = (HERE / 'prompts' / 'planner_system.txt').read_text(encoding='utf-8')
    repo_map = (HERE / 'fixtures' / 'miro_map.txt').read_text(encoding='utf-8')
    args.output.mkdir(parents=True, exist_ok=True)
    for record in records:
        if record['id'] not in wanted:
            continue
        if args.skip_existing and (args.output / f"{record['id']}.json").exists():
            print(record['id'], 'cached')
            continue
        user = (f"task_id: {record['id']}\n\nTASK:\n{record['prompt']}\n\n"
                f"REPO MODULE MAP (miro-clone, digest only -- no source):\n{repo_map}")
        payload = json.dumps({'model': MODEL, 'messages': [
            {'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            'response_format': {'type': 'text'}, 'temperature': 0,
            'max_tokens': 10000}).encode('utf-8')
        req = urllib.request.Request(URL, data=payload,
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=240) as response:
                data = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')[:1000]
            raise RuntimeError(f'local model HTTP {exc.code}: {detail}') from exc
        content = data['choices'][0]['message'].get('content') or ''
        if not content.strip():
            payload = json.dumps({'model': MODEL, 'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user + '\nReturn only the requested JSON object; do not use hidden reasoning.'}],
                'response_format': {'type': 'text'}, 'temperature': 0,
                'max_tokens': 12000}).encode('utf-8')
            req = urllib.request.Request(URL, data=payload,
                headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=240) as response:
                data = json.loads(response.read().decode('utf-8'))
            content = data['choices'][0]['message'].get('content') or ''
        if not content.strip():
            raise RuntimeError(f"local model returned empty content for {record['id']}")
        plan = _extract_json(content)
        plan.setdefault('task_id', record['id'])
        plan['planner_model'] = MODEL
        plan.setdefault('task', record['prompt'])
        plan = mark_big_files(plan, {'src/board/Board.tsx', 'src/board/toolbars.tsx',
                                     'src/board/useStickyNotes.ts', 'src/board/Board.test.tsx'})
        problems = validate_plan(plan)
        (args.output / f"{record['id']}.json").write_text(
            json.dumps(plan, indent=2), encoding='utf-8')
        print(record['id'], 'valid' if not problems else 'INVALID', len(plan.get('steps', [])),
              '; '.join(problems))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
