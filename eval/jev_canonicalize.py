"""Canonicalize jegrep hits against AST-entity ids of the dependency graph.

TASK-003 acceptance criterion (3): in the Jev arm, jegrep's raw ranking and its
text snippets must NEVER reach the executor. ``locagent_mcp._jegrep_hits``
today (see ``locagent_mcp.py`` lines ~491-524) only validates a hit's path
against a *file* node -- it never resolves down to the AST entities the graph
actually indexes (functions/classes/contexts), so a "hit" can still be an
opaque whole-file blob handed to the executor.

This module re-runs the same external ``jegrep`` call, then canonicalizes each
raw hit through two required steps before it is allowed into the package that
reaches the executor:

1. the hit path must resolve to a ``NODE_TYPE_FILE`` node of the dependency
   graph built for the target worktree (same check as ``_jegrep_hits``);
2. that file node must have at least one AST entity child (function, class, or
   react context -- ``contains`` edges, non-nested, same population
   ``graph_semantic_search`` lists for a human) -- the *entity ids*, not the
   file id, are what enters the canonical package.

Any raw hit that fails either step is dropped and recorded as a guard
rejection with a reason; only file+entity path/id pairs -- never jegrep's raw
JSON, scores as free text, or code snippets -- survive into
``canonical_entities``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dependency_graph.build_graph import (
    NODE_TYPE_CLASS,
    NODE_TYPE_CONTEXT,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
)

JEGREP_TIMEOUT_S = 30
_ENTITY_TYPES = (NODE_TYPE_FUNCTION, NODE_TYPE_CLASS, NODE_TYPE_CONTEXT)


class JegrepUnavailable(RuntimeError):
    """``jegrep`` is missing, timed out, or returned malformed JSON."""


def raw_jegrep_hits(query: str, repo: Path) -> List[Dict[str, Any]]:
    """Run external ``jegrep`` and return its hits verbatim (pre-canonicalization).

    Raises ``JegrepUnavailable`` on any failure -- callers decide the fallback
    (TASK-003 requires the Jev arm to fail closed into a guard rejection, never
    to silently leak an un-canonicalized hit).
    """
    exe = shutil.which('jegrep')
    if exe is None:
        raise JegrepUnavailable('jegrep not found on PATH')
    try:
        proc = subprocess.run(
            [exe, query, '.', '--json', '--quiet'],
            cwd=str(repo), capture_output=True, text=True, timeout=JEGREP_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JegrepUnavailable(f'jegrep invocation failed: {exc}') from exc
    if proc.returncode != 0:
        raise JegrepUnavailable(f'jegrep exited {proc.returncode}: {proc.stderr[-500:]}')
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise JegrepUnavailable(f'jegrep produced non-JSON output: {exc}') from exc
    return list(data.get('hits') or [])


def _normalize_path(raw_path: str, repo: Path) -> str | None:
    p = str(raw_path).replace('\\', '/').strip()
    while p.startswith('./'):
        p = p[2:]
    if not p:
        return None
    if Path(p).is_absolute():
        try:
            p = Path(p).resolve().relative_to(repo).as_posix()
        except (ValueError, OSError):
            return None
    return p


def _entity_children(graph, file_id: str) -> List[str]:
    """Top-level AST entity ids ``contains``-attached to ``file_id``."""
    return sorted(
        v for _, v, ed in graph.out_edges(file_id, data=True)
        if ed.get('type') == 'contains'
        and graph.nodes.get(v, {}).get('type') in _ENTITY_TYPES
        and ':' in v
        and '.' not in v.split(':', 1)[1]
    )


def canonicalize_hits(
    raw_hits: List[Dict[str, Any]], graph, repo: Path,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Canonicalize raw jegrep hits into AST-entity ids.

    Returns ``(canonical_entities, guard_rejections)``. ``canonical_entities``
    is a de-duplicated, first-seen-order list of ``path:Name`` entity ids --
    never a file id alone, never jegrep score/snippet text. Every rejected raw
    hit is recorded with its original (untrusted) path and a machine-readable
    reason so the result artifact can report a guard-rejection count.
    """
    canonical: List[str] = []
    rejections: List[Dict[str, Any]] = []
    seen = set()
    for hit in raw_hits:
        raw_path = str(hit.get('path', ''))
        norm = _normalize_path(raw_path, repo)
        if norm is None:
            rejections.append({'raw_path': raw_path, 'reason': 'outside_repo_or_empty'})
            continue
        nd = graph.nodes.get(norm)
        if nd is None or nd.get('type') != NODE_TYPE_FILE:
            rejections.append({'raw_path': raw_path, 'normalized_path': norm,
                               'reason': 'not_a_graph_file_node'})
            continue
        entities = _entity_children(graph, norm)
        if not entities:
            rejections.append({'raw_path': raw_path, 'normalized_path': norm,
                               'reason': 'file_node_has_no_ast_entities'})
            continue
        for eid in entities:
            if eid not in seen:
                seen.add(eid)
                canonical.append(eid)
    return canonical, rejections


def canonicalize_query(query: str, repo: Path, graph) -> Tuple[List[str], List[Dict[str, Any]], int]:
    """End-to-end: run jegrep, canonicalize. Returns
    ``(canonical_entities, guard_rejections, raw_hit_count)``.

    If jegrep itself is unavailable/fails, every "hit" is trivially zero and
    the whole query counts as one guard rejection (fail closed, never fall
    back to an un-canonicalized signal -- that fallback belongs to
    ``graph_semantic_search``, not to this canonical-handoff path)."""
    try:
        raw = raw_jegrep_hits(query, repo)
    except JegrepUnavailable as exc:
        return [], [{'raw_path': None, 'reason': f'jegrep_unavailable: {exc}'}], 0
    canonical, rejections = canonicalize_hits(raw, graph, repo)
    return canonical, rejections, len(raw)
