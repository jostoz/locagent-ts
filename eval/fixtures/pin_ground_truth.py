"""Re-pin ``eval/miro_clone_localization.jsonl`` against miro-clone's live HEAD.

miro-clone is a working repo -- Sonnet has edited it, so recorded line numbers
drift (q1's ``handleAiAction`` moved 6077 -> 6078). The same eval scripts then
score differently on different days. This helper resolves every ``gt_entities``
id through ``locagent_mcp`` itself, prints an old -> current line diff, checks
each ``answer_tokens`` bundle against the current source, and reports HEAD so a
human can bump ``pinned_at``.

It does NOT rewrite the jsonl -- the corpus is hand-maintained. Read the report,
edit the file, set ``pinned_at`` to the printed short sha.

    python -m eval.fixtures.pin_ground_truth
    python -m eval.fixtures.pin_ground_truth --repo D:/checkouts/miro-clone --json

Env (defaults match ``~/.cline/data/settings/cline_mcp_settings.json``):
    LOCAGENT_REPO        C:/Users/joz/Documents/miro-clone
    LOCAGENT_CACHE_DIR   <locagent-ts>/.locagent-cache/miro-clone
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

_HERE = Path(__file__).resolve()
_LOCAGENT_TS = _HERE.parents[2]                       # eval/fixtures/x.py -> repo root
_DEFAULT_REPO = r'C:/Users/joz/Documents/miro-clone'
_DEFAULT_CACHE = str(_LOCAGENT_TS / '.locagent-cache' / 'miro-clone')
_CORPUS = _LOCAGENT_TS / 'eval' / 'miro_clone_localization.jsonl'


def _git_head(repo: str) -> str:
    try:
        out = subprocess.run(
            ['git', '-C', repo, 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or '?'
    except Exception as e:  # noqa: BLE001
        return f'?({e})'


def _load_corpus(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def _import_locagent(repo: str, cache: str):
    os.environ['LOCAGENT_REPO'] = repo
    os.environ['LOCAGENT_CACHE_DIR'] = cache
    if str(_LOCAGENT_TS) not in sys.path:
        sys.path.insert(0, str(_LOCAGENT_TS))
    import locagent_mcp as L  # noqa: E402  -- env must be set first
    with L._protect_stdout():
        L._ensure_loaded()
    return L


def _entity_lines(L, ent: str) -> Dict[str, object]:
    nid, sugg = L._resolve_id(ent)
    if nid is None:
        return {'input': ent, 'resolved': None, 'suggestions': sugg[:8]}
    nd = L._STATE['graph'].nodes[nid]
    return {
        'input': ent,
        'resolved': nid,
        'file': nid.split(':')[0],
        'start_line': nd.get('start_line'),
        'end_line': nd.get('end_line'),
        'type': nd.get('type'),
        'code': nd.get('code', '') or '',
    }


def _frag_kind(frag: str) -> str:
    """A *locator* fragment targets the model's prose answer (a bare line number,
    a filename) -- a non-match against source is expected, not a ground-truth
    problem. A *content* fragment carries a real identifier; a miss there means
    the gt entity does not contain what the corpus claims."""
    core = frag.replace(r'\b', '')                 # drop word-boundary anchors
    if re.search(r'\.(tsx?|jsx?|py|md)\b', core):  # a filename
        return 'locator'
    if not re.search(r'[A-Za-z]', core):           # digits / punctuation only
        return 'locator'
    return 'content'


def _answer_token_report(bundle: str, haystack: str) -> List[dict]:
    out = []
    for frag in (t.strip() for t in bundle.split(';;') if t.strip()):
        kind = _frag_kind(frag)
        try:
            ok = bool(re.search(frag, haystack, re.I))
        except re.error as e:
            out.append({'fragment': frag, 'kind': kind, 'match': None,
                        'regex_error': str(e)})
            continue
        # a matched locator or any content fragment reports its real result;
        # an unmatched locator is expected (it targets the answer prose).
        out.append({'fragment': frag, 'kind': kind, 'match': ok,
                    'is_problem': (kind == 'content' and not ok)})
    return out


def pin(repo: str, cache: str, corpus_path: Path) -> dict:
    L = _import_locagent(repo, cache)
    head = _git_head(repo)
    records = _load_corpus(corpus_path)

    report: dict = {
        'repo': repo, 'cache': cache, 'head': head,
        'corpus': str(corpus_path), 'n_records': len(records),
        'graph_nodes': L._STATE['graph'].number_of_nodes(),
        'records': [], 'summary': {},
    }
    n_unresolved = n_line_drift = n_token_miss = n_stale_pin = 0

    for r in records:
        rid = r.get('id', '?')
        rr: dict = {'id': rid, 'category': r.get('category'), 'entities': [],
                    'line_diff': [], 'answer_tokens': [], 'pin_current': r.get('pinned_at')}
        if r.get('pinned_at') not in (head, None, ''):
            n_stale_pin += 1
            rr['pin_stale'] = True

        gt_lines: dict = r.get('gt_lines') or {}
        combined_code = []
        # answer_tokens fragments are often bare line numbers ("6091") that live
        # in the *file* but outside any single gt entity -- fold the whole file
        # of each gt_file into the haystack so the pin check is not a false miss.
        for gtf in (r.get('gt_files') or []):
            fnid, _ = L._resolve_id(gtf)
            if fnid is not None:
                combined_code.append(L._STATE['graph'].nodes[fnid].get('code', '') or '')
        for ent in (r.get('gt_entities') or []):
            info = _entity_lines(L, ent)
            rr['entities'].append(info)
            if info['resolved'] is None:
                n_unresolved += 1
                continue
            combined_code.append(info['code'])
            # compare against any recorded line for this file (file-type
            # entities have no start_line -- nothing to drift-check)
            rec_lines = gt_lines.get(info['file']) or []
            cur = info['start_line']
            if rec_lines and cur is not None:
                if cur not in rec_lines:
                    n_line_drift += 1
                    rr['line_diff'].append({
                        'file': info['file'], 'entity': info['resolved'],
                        'recorded': rec_lines, 'current_start': cur,
                        'current_end': info['end_line'],
                    })

        bundle = r.get('answer_tokens')
        if bundle:
            # haystack: gt entity + gt file source, plus a locator blob (the
            # file paths and every recorded line number) so path/line fragments
            # can verify against something real.
            locator_blob = ' '.join(
                list(r.get('gt_files') or [])
                + [str(n) for nums in gt_lines.values() for n in nums]
                + [r.get('notes', '')]
            )
            hay = '\n'.join(combined_code) + '\n' + locator_blob
            toks = _answer_token_report(bundle, hay)
            rr['answer_tokens'] = toks
            if any(t.get('is_problem') or t['match'] is None for t in toks):
                n_token_miss += 1

        report['records'].append(rr)

    report['summary'] = {
        'unresolved_entities': n_unresolved,
        'records_with_line_drift': n_line_drift,
        'records_with_token_miss': n_token_miss,
        'records_with_stale_pin': n_stale_pin,
        'head': head,
    }
    return report


def _print_human(report: dict) -> None:
    s = report['summary']
    print(f"repo   {report['repo']}")
    print(f"cache  {report['cache']}")
    print(f"HEAD   {report['head']}   graph: {report['graph_nodes']} nodes   "
          f"corpus: {report['n_records']} records")
    print('=' * 72)
    for rr in report['records']:
        flags = []
        if rr.get('pin_stale'):
            flags.append(f"PIN STALE ({rr['pin_current']} -> {report['head']})")
        unresolved = [e for e in rr['entities'] if e['resolved'] is None]
        if unresolved:
            flags.append(f'{len(unresolved)} UNRESOLVED')
        if rr['line_diff']:
            flags.append(f'{len(rr["line_diff"])} LINE DRIFT')
        tok_problem = [t for t in rr['answer_tokens']
                       if t.get('is_problem') or t['match'] is None]
        tok_unverified = [t for t in rr['answer_tokens']
                          if t.get('kind') == 'locator' and t['match'] is False]
        if tok_problem:
            flags.append(f'{len(tok_problem)} TOKEN MISS')
        if tok_unverified:
            flags.append(f'{len(tok_unverified)} locator unverified')
        head = f"{rr['id']:<6} {rr['category'] or '':<28}"
        print(f"{head} {'  |  '.join(flags) if flags else 'ok'}")
        for e in rr['entities']:
            if e['resolved'] is None:
                print(f"       ! {e['input']!r} did not resolve; suggestions:")
                for sug in e['suggestions']:
                    print(f"           {sug}")
            else:
                print(f"       - {e['resolved']}  L{e['start_line']}-{e['end_line']}")
        for d in rr['line_diff']:
            print(f"       ~ {d['file']}: recorded {d['recorded']} -> current "
                  f"start {d['current_start']} (end {d['current_end']})")
        for t in tok_problem:
            why = t.get('regex_error', 'content fragment absent from gt entity source')
            print(f"       x answer_token /{t['fragment']}/  -- {why}")
        for t in tok_unverified:
            print(f"       ? answer_token /{t['fragment']}/  -- locator, not in "
                  f"source/paths/lines/notes (may still be right in the model's prose)")
    print('=' * 72)
    print(f"unresolved={s['unresolved_entities']}  line_drift={s['records_with_line_drift']}  "
          f"token_miss={s['records_with_token_miss']}  stale_pin={s['records_with_stale_pin']}")
    if any(s[k] for k in ('unresolved_entities', 'records_with_line_drift',
                          'records_with_token_miss', 'records_with_stale_pin')):
        print(f"\n-> edit {report['corpus']}: fix gt_entities / gt_lines / answer_tokens,")
        print(f"   then set  \"pinned_at\": \"{report['head']}\"  on every touched record.")
    else:
        print('\n-> corpus is pinned to HEAD, nothing to do.')


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', default=os.environ.get('LOCAGENT_REPO', _DEFAULT_REPO))
    ap.add_argument('--cache', default=os.environ.get('LOCAGENT_CACHE_DIR', _DEFAULT_CACHE))
    ap.add_argument('--corpus', default=str(_CORPUS))
    ap.add_argument('--json', action='store_true', help='dump the raw report as JSON')
    args = ap.parse_args(argv)

    report = pin(args.repo, args.cache, Path(args.corpus))
    if args.json:
        # entity 'code' is bulky -- drop it from the JSON dump
        for rr in report['records']:
            for e in rr['entities']:
                e.pop('code', None)
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)

    s = report['summary']
    stale = any(s[k] for k in ('unresolved_entities', 'records_with_line_drift',
                               'records_with_token_miss', 'records_with_stale_pin'))
    return 1 if stale else 0


if __name__ == '__main__':
    sys.exit(main())
