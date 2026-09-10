"""Torch-free re-implementation of the acc@k / recall@k / precision@k that
``evaluation/eval_metric.py`` computes -- that module drags in ``torch`` +
``datasets`` + ``pandas`` just to slice a few label vectors. Here the same
numbers come out of plain set arithmetic on ranked prediction lists.

Definitions (mirrors upstream ``cal_metrics_w_*`` with de-duplicated preds):

    acc@k     mean over records of  1.0 if  |top-k preds ∩ gt| == min(|gt|, k)  else 0.0
    recall@k  mean over records of  |top-k preds ∩ gt| / |gt|
    precision@k  mean over records of  |top-k preds ∩ gt| / k

``acc@k`` is the strict "did the run surface the whole ground-truth set within
its first k picks" score (when |gt| <= k); upstream's label-vector form reduces
to exactly this once predictions are de-duplicated, which they always are here
(a ranked list keeps first-seen order, drops repeats).

Run ``python -m eval.metrics --selftest`` for the hand-computed fixtures.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, Iterable, List, Sequence

import numpy as np

DEFAULT_KS = (1, 3, 5, 10)


# --------------------------------------------------------------------- helpers
def _dedupe(seq: Iterable[str]) -> List[str]:
    """First-seen order, repeats dropped."""
    seen: set = set()
    out: List[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def to_file_level(entity_ranked: Sequence[str]) -> List[str]:
    """``'src/a.tsx:Foo'`` -> ``'src/a.tsx'``; de-duped, order preserved.

    An id with no ``':'`` (already a bare path) passes through untouched. The
    split is on the *first* ``':'`` so a Windows drive letter would survive, but
    corpus ids are always repo-relative POSIX paths.
    """
    return _dedupe(e.split(':', 1)[0] for e in entity_ranked)


# --------------------------------------------------------------------- metrics
def acc_at_k(preds: Sequence[str], gts: Sequence[str], k: int) -> float:
    gtset = set(gts)
    if not gtset:
        return 0.0
    topk = _dedupe(preds)[:k]
    hits = sum(1 for p in topk if p in gtset)
    return 1.0 if hits == min(len(gtset), k) else 0.0


def recall_at_k(preds: Sequence[str], gts: Sequence[str], k: int) -> float:
    gtset = set(gts)
    if not gtset:
        return 0.0
    topk = _dedupe(preds)[:k]
    hits = sum(1 for p in topk if p in gtset)
    return hits / len(gtset)


def precision_at_k(preds: Sequence[str], gts: Sequence[str], k: int) -> float:
    if k <= 0:
        return 0.0
    gtset = set(gts)
    topk = _dedupe(preds)[:k]
    hits = sum(1 for p in topk if p in gtset)
    return hits / k


_METRIC_FNS = {'acc': acc_at_k, 'recall': recall_at_k, 'precision': precision_at_k}


def _mean(vals: List[float]) -> float:
    return float(np.mean(vals)) if vals else 0.0


def evaluate(
    records: Sequence[dict],
    predictions: Dict[str, dict],
    ks: Sequence[int] = DEFAULT_KS,
    metrics: Sequence[str] = ('acc', 'recall', 'precision'),
) -> dict:
    """Score a run.

    ``records``    -- corpus rows; each needs ``id``, ``gt_files``, ``gt_entities``.
                     A row with an empty ``gt_entities`` is skipped for the
                     entity level but still scored at file level (mirrors
                     upstream's ``if not gt_dict[instance_id]: continue`` per
                     level).
    ``predictions`` -- ``{id: {"ranked_entities": [...], "ranked_files": [...]}}``.
                     A missing id scores as an empty prediction (all-zero row),
                     exactly like upstream.

    Returns ``{"file": {"acc@1": ..., ...}, "entity": {...}, "n_file": int,
    "n_entity": int}``.
    """
    out: dict = {'file': {}, 'entity': {}}
    file_rows = [r for r in records if r.get('gt_files')]
    ent_rows = [r for r in records if r.get('gt_entities')]
    out['n_file'] = len(file_rows)
    out['n_entity'] = len(ent_rows)

    for level, rows, gt_key, pred_key in (
        ('file', file_rows, 'gt_files', 'ranked_files'),
        ('entity', ent_rows, 'gt_entities', 'ranked_entities'),
    ):
        if not rows:
            continue
        for m in metrics:
            fn = _METRIC_FNS[m]
            for k in ks:
                vals: List[float] = []
                for r in rows:
                    pred = predictions.get(r['id']) or {}
                    ranked = pred.get(pred_key)
                    if ranked is None and level == 'file':
                        # allow a run to report only entities; derive files.
                        ranked = to_file_level(pred.get('ranked_entities') or [])
                    vals.append(fn(ranked or [], r[gt_key], k))
                out[level][f'{m}@{k}'] = round(_mean(vals), 4)
    return out


# --------------------------------------------------------------------- selftest
def selftest() -> None:
    """Three hand-computed fixtures. Raises AssertionError on mismatch."""

    # 1) single-entity gt, hit at rank 1  -> everything 1.0 / recall trivially 1
    assert acc_at_k(['a', 'b'], ['a'], 1) == 1.0
    assert recall_at_k(['a', 'b'], ['a'], 1) == 1.0
    assert precision_at_k(['a', 'b'], ['a'], 1) == 1.0
    assert precision_at_k(['a', 'b'], ['a'], 2) == 0.5

    # 2) two-entity gt, one found in top-3, other at rank 5
    preds = ['x', 'g1', 'y', 'z', 'g2']
    gts = ['g1', 'g2']
    assert acc_at_k(preds, gts, 3) == 0.0          # only 1 of 2 in top-3
    assert round(recall_at_k(preds, gts, 3), 4) == 0.5
    assert acc_at_k(preds, gts, 5) == 1.0          # both in top-5
    assert recall_at_k(preds, gts, 5) == 1.0
    # |gt| = 2 > k = 1  -> acc@1 needs 1 hit at rank 1; 'x' is a miss
    assert acc_at_k(preds, gts, 1) == 0.0
    # gt entirely inside top-2 preds ['x','g1'] would need both; here only g1
    assert acc_at_k(['g1', 'g2', 'q'], gts, 1) == 1.0   # min(|gt|,1)=1, 1 hit

    # 3) file-level projection + de-dupe + missing prediction
    recs = [
        {'id': 'r1', 'gt_files': ['src/a.tsx'],
         'gt_entities': ['src/a.tsx:Foo', 'src/a.tsx:bar']},
        {'id': 'r2', 'gt_files': ['src/b.tsx'], 'gt_entities': ['src/b.tsx:Baz']},
    ]
    preds_map = {
        'r1': {'ranked_entities': ['src/a.tsx:Foo', 'src/a.tsx:Foo',
                                   'src/a.tsx:bar', 'src/c.tsx:Qux']},
        # r2 absent -> empty prediction
    }
    res = evaluate(recs, preds_map, ks=(1, 3))
    assert res['n_file'] == 2 and res['n_entity'] == 2
    # file level: r1 top-1 file = src/a.tsx (hit), r2 = miss -> acc@1 = 0.5
    assert res['file']['acc@1'] == 0.5
    # entity level r1: de-duped preds = [Foo, bar, Qux]; gt = {Foo, bar}
    #   acc@3 -> 2 hits == min(2,3) -> 1.0 ; r2 -> 0.0 ; mean = 0.5
    assert res['entity']['acc@3'] == 0.5
    assert res['entity']['recall@3'] == 0.5   # r1 recall 1.0, r2 recall 0.0

    # empty everything
    assert evaluate([], {}, ks=(1,))['file'] == {}
    print('eval.metrics selftest: OK')


# ------------------------------------------------------------------------- cli
def _load_jsonl(path: str) -> List[dict]:
    with open(path, encoding='utf-8') as fh:
        return [json.loads(ln) for ln in fh if ln.strip()]


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--gt', help='corpus jsonl (eval/miro_clone_localization.jsonl)')
    ap.add_argument('--pred', help='prediction jsonl: {"id":..,"ranked_entities":[..],"ranked_files":[..]}')
    ap.add_argument('--ks', default='1,3,5,10')
    args = ap.parse_args(argv)

    if args.selftest:
        selftest()
        return 0

    if not (args.gt and args.pred):
        ap.error('need --gt and --pred (or --selftest)')

    ks = tuple(int(x) for x in args.ks.split(','))
    records = _load_jsonl(args.gt)
    preds = {r['id']: r for r in _load_jsonl(args.pred)}
    res = evaluate(records, preds, ks=ks)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
