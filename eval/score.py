"""Answer-token scorer + corpus validator.

``score_answer`` is the ``eval_v4.sh`` ``score_answer()`` shell helper lifted
into Python unchanged in spirit: an ``answer_tokens`` string is a bundle of
``;;``-separated regex fragments; the answer text scores ``hits / n`` and is
"correct" only when every fragment matches (case-insensitive, ``re.search``).

``validate_corpus`` is the Stage-0 gate: every record well-formed, category in
the allowed namespace, localize rows carry ``gt_entities`` + ``answer_tokens``,
edit rows carry a non-empty runnable ``acceptance`` list.

    python -m eval.score --validate-corpus
    python -m eval.score --answer path/to/transcript.txt --tokens "handleAiAction ;; 6078"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import List, Tuple

CORPUS = 'eval/miro_clone_localization.jsonl'

_LOCALIZE_CATS = {
    'localize/focused-wiring',
    'localize/focused-def',
    'localize/broad-enumerate',
    'localize/callers',
    'localize/renders-upstream',
    'localize/comment-convention',
    'localize/cross-file-import',
    'localize/disambiguation',
    'localize/mixed',
}
_EDIT_CATS = {
    'edit/add-interface-field',
    'edit/new-isolated-file',
    'edit/small-inplace',
    'edit/thread-prop',
    'edit/extend-check',
}
ALLOWED_CATS = _LOCALIZE_CATS | _EDIT_CATS

_REQUIRED_COMMON = ('id', 'category', 'prompt', 'gt_files', 'pinned_at')


# --------------------------------------------------------------------- scoring
def _bundle(tokens: str) -> List[str]:
    return [t.strip() for t in tokens.split(';;') if t.strip()]


def score_answer(answer_text: str, answer_tokens: str) -> Tuple[float, bool]:
    """Return ``(fraction_matched, all_matched)``."""
    toks = _bundle(answer_tokens)
    if not toks:
        return 0.0, False
    hits = sum(1 for t in toks if re.search(t, answer_text, re.I))
    return hits / len(toks), hits == len(toks)


# ------------------------------------------------------------------ validation
def _load_jsonl(path: str) -> List[dict]:
    with open(path, encoding='utf-8') as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def validate_corpus(path: str = CORPUS) -> List[str]:
    """Return a list of human-readable problems; empty list == corpus is clean."""
    problems: List[str] = []
    try:
        records = _load_jsonl(path)
    except FileNotFoundError:
        return [f'{path}: not found']
    except json.JSONDecodeError as e:
        return [f'{path}: bad JSON -- {e}']

    if not records:
        return [f'{path}: empty']

    seen_ids: set = set()
    for i, r in enumerate(records):
        tag = f'record {i} (id={r.get("id", "?")})'
        for key in _REQUIRED_COMMON:
            if not r.get(key):
                problems.append(f'{tag}: missing/empty "{key}"')

        rid = r.get('id')
        if rid in seen_ids:
            problems.append(f'{tag}: duplicate id')
        seen_ids.add(rid)

        cat = r.get('category')
        if cat not in ALLOWED_CATS:
            problems.append(f'{tag}: category "{cat}" not in allowed set')

        if not isinstance(r.get('gt_files'), list) or not r.get('gt_files'):
            problems.append(f'{tag}: gt_files must be a non-empty list')

        is_edit = isinstance(cat, str) and cat.startswith('edit/')
        if is_edit:
            acc = r.get('acceptance')
            if not isinstance(acc, list) or not acc:
                problems.append(f'{tag}: edit record needs a non-empty "acceptance" list')
            elif not all(isinstance(c, str) and c.strip() for c in acc):
                problems.append(f'{tag}: "acceptance" entries must be non-empty strings')
        else:  # localize
            if not isinstance(r.get('gt_entities'), list) or not r.get('gt_entities'):
                problems.append(f'{tag}: localize record needs a non-empty "gt_entities" list')
            if not r.get('answer_tokens'):
                problems.append(f'{tag}: localize record needs "answer_tokens"')
            else:
                # every fragment must be a compilable regex
                for frag in _bundle(r['answer_tokens']):
                    try:
                        re.compile(frag)
                    except re.error as e:
                        problems.append(f'{tag}: answer_tokens fragment {frag!r} is not valid regex -- {e}')

        gl = r.get('gt_lines')
        if gl is not None and not isinstance(gl, dict):
            problems.append(f'{tag}: gt_lines must be an object mapping file -> [lines]')

    return problems


# ------------------------------------------------------------------------- cli
def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--validate-corpus', action='store_true')
    ap.add_argument('--corpus', default=CORPUS)
    ap.add_argument('--answer', help='path to an answer/transcript text file')
    ap.add_argument('--tokens', help='";;"-separated regex bundle')
    args = ap.parse_args(argv)

    if args.validate_corpus:
        problems = validate_corpus(args.corpus)
        if problems:
            print(f'{len(problems)} problem(s) in {args.corpus}:')
            for p in problems:
                print(f'  - {p}')
            return 1
        n = len(_load_jsonl(args.corpus))
        print(f'{args.corpus}: OK ({n} records)')
        return 0

    if args.answer and args.tokens:
        txt = open(args.answer, encoding='utf-8', errors='replace').read()
        frac, ok = score_answer(txt, args.tokens)
        print(f'{frac:.3f} {"correct" if ok else "incomplete"}')
        return 0 if ok else 1

    ap.error('need --validate-corpus or (--answer and --tokens)')
    return 2


if __name__ == '__main__':
    sys.exit(main())
