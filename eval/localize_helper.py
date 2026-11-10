"""Subprocess-isolated localization step for ``eval.run_jev_vs_bm25``.

Builds the dependency graph for exactly one worktree and returns a ranked,
AST-canonical entity package for one of the two arms:

* ``bm25`` -- ``dependency_graph.ts_bm25.TsBM25Index`` retrieval. The index is
  built directly over the graph's function/class/context/file nodes (see
  ``TsBM25Index.from_graph``), so its ranked ids are already AST entities --
  "Gestado+BM25+AST" needs no extra canonicalization step, only the same
  entity-level index ``graph_search`` uses internally.
* ``jev`` -- external ``jegrep`` + ``eval.jev_canonicalize.canonicalize_query``.
  Only canonicalized entity ids leave this process; jegrep's raw hits, scores,
  and any text it printed never appear in this script's output.

Runs as a fresh subprocess per call (not imported in-process by the runner)
so ``LOCAGENT_REPO``-style globals never leak between the two arms' worktrees,
and so a graph build for one arm can never accidentally reuse another arm's
in-memory graph.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# Some deps print at import time (e.g. "resource module not available on
# Windows" -- see locagent_mcp.py's own note on this same wart). This
# script's stdout is a single-line JSON contract for eval.run_jev_vs_bm25's
# subprocess call: redirect stdout to stderr for the whole import + build,
# restore it only in main() to emit the final JSON line.
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

from dependency_graph.build_graph import (  # noqa: E402
    NODE_TYPE_CLASS,
    NODE_TYPE_CONTEXT,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
)
from dependency_graph.ts_build_graph import build_ts_graph  # noqa: E402
from dependency_graph.ts_bm25 import TsBM25Index  # noqa: E402

from eval.jev_canonicalize import canonicalize_query  # noqa: E402

_ENTITY_TYPES = (NODE_TYPE_FUNCTION, NODE_TYPE_CLASS, NODE_TYPE_CONTEXT)


def _entity_brief(graph, nid: str) -> Dict[str, Any]:
    nd = graph.nodes.get(nid, {})
    head = ''
    if nd.get('type') != NODE_TYPE_FILE:
        skeleton_lines = (nd.get('skeleton') or '').splitlines()
        head = next((ln.strip() for ln in skeleton_lines if ln.strip()
                    and not ln.strip().startswith(('//', '/*', '*'))), '')
    return {
        'id': nid,
        'file': nid.split(':', 1)[0],
        'type': nd.get('type'),
        'start_line': nd.get('start_line'),
        'end_line': nd.get('end_line'),
        'head': head[:160],
    }


def _bm25_arm(graph, query: str, max_results: int) -> Dict[str, Any]:
    index = TsBM25Index.from_graph(graph)
    ranked = index.retrieve(query, k=max_results)
    return {
        'arm': 'bm25',
        'mechanism': 'dependency_graph.ts_bm25.TsBM25Index.retrieve',
        'ranked_entities': ranked[:max_results],
        'guard_rejections': [],
        'raw_hit_count': len(ranked),
    }


def _jev_arm(graph, repo: Path, query: str, max_results: int) -> Dict[str, Any]:
    canonical, rejections, raw_count = canonicalize_query(query, repo, graph)
    return {
        'arm': 'jev',
        'mechanism': 'eval.jev_canonicalize.canonicalize_query',
        'ranked_entities': canonical[:max_results],
        'guard_rejections': rejections,
        'raw_hit_count': raw_count,
    }


def run(repo: Path, arm: str, query: str, max_results: int) -> Dict[str, Any]:
    graph = build_ts_graph(str(repo), fuzzy_search=False)
    if arm == 'bm25':
        result = _bm25_arm(graph, query, max_results)
    elif arm == 'jev':
        result = _jev_arm(graph, repo, query, max_results)
    else:
        raise ValueError(f'unknown arm {arm!r}')
    result['entities'] = [_entity_brief(graph, nid) for nid in result['ranked_entities']]
    result['canonical_package_bytes'] = len(
        json.dumps(result['entities'], ensure_ascii=False).encode('utf-8'))
    return result


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', required=True, type=Path)
    ap.add_argument('--arm', required=True, choices=('bm25', 'jev'))
    ap.add_argument('--query', required=True)
    ap.add_argument('--max-results', type=int, default=15)
    args = ap.parse_args(argv)
    result = run(args.repo.resolve(), args.arm, args.query, args.max_results)
    print(json.dumps(result, ensure_ascii=False), file=_REAL_STDOUT)
    return 0


if __name__ == '__main__':
    sys.exit(main())
