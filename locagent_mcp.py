#!/usr/bin/env python
"""LocAgent-TS -- MCP server for graph-guided code localization over ONE repo.

The repo is the server's working directory (or ``$LOCAGENT_REPO``). On first use
it builds the tree-sitter code graph + a BM25 index for that repo and caches them
under ``<repo>/.locagent/`` (or ``$LOCAGENT_CACHE_DIR`` -- use this for a
read-only tree); subsequent starts reload the cache unless the set of source
files (path / mtime / size) changed. A repo it cannot write to just runs without
a cache.

Tools:
  search_code_entities(query, max_results=10, scope="all")
  get_entity(entity_id, mode="skeleton"|"full")
  traverse(entity_id, edge_types=None, direction="both", hops=2)
  get_repo_overview(max_depth=3)

Wire into Cline (`~/.cline/data/settings/cline_mcp_settings.json`):
  "locagent": {
    "command": "C:/Users/joz/orca/projects/locagent-ts/.venv/Scripts/python.exe",
    "args": ["C:/Users/joz/orca/projects/locagent-ts/locagent_mcp.py"],
    "cwd": "C:/path/to/the/target/worktree"
  }
"""

import contextlib
import hashlib
import logging
import os
import pickle
import sys
import warnings
from pathlib import Path
from typing import List, Optional, Tuple

# NB: do NOT `from __future__ import annotations` here -- this mcp (1.13.x)
# introspects raw parameter annotations and chokes on stringified generics.

warnings.filterwarnings('ignore')
logging.getLogger('bm25s').setLevel(logging.WARNING)

# MCP speaks newline-delimited JSON-RPC over stdout. Some deps print to stdout at
# IMPORT time (e.g. "resource module not available on Windows"), which a strict
# client like OMP rejects with "Failed to parse JSONL". Redirect stdout to stderr
# for the whole module load + tool registration; restore it just before
# mcp.run() hands stdout to the protocol.
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr


@contextlib.contextmanager
def _protect_stdout():
    """Route to stderr anything a graph/index build (bm25s, stray prints) writes
    to stdout while the MCP protocol owns it -- used around lazy work inside tool
    handlers."""
    saved = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = saved


# this file lives at the repo root -- make the package importable regardless of cwd
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
if _SELF_DIR not in sys.path:
    sys.path.insert(0, _SELF_DIR)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from dependency_graph.build_graph import (  # noqa: E402
    NODE_TYPE_CLASS,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    VALID_EDGE_TYPES,
)
from dependency_graph.ts_bm25 import TsBM25Index  # noqa: E402
from dependency_graph.ts_build_graph import SKIP_DIRS, SOURCE_EXTS, build_ts_graph  # noqa: E402
from dependency_graph.traverse_graph import (  # noqa: E402
    RepoEntitySearcher,
    is_test_file,
    traverse_tree_structure,
)
from plugins.location_tools.retriever.fuzzy_retriever import (  # noqa: E402
    fuzzy_retrieve_from_graph_nodes,
)
from plugins.location_tools.utils.compress_file_ts import get_skeleton  # noqa: E402

# ─────────────────────────────  repo / cache  ────────────────────────────────

REPO = Path(os.environ.get('LOCAGENT_REPO', os.getcwd())).resolve()
# cache lives in the target repo by default; override for read-only trees.
CACHE_DIR = Path(os.environ['LOCAGENT_CACHE_DIR']).resolve() \
    if os.environ.get('LOCAGENT_CACHE_DIR') else REPO / '.locagent'
_CACHE_SCHEMA = 'v1'          # bump to invalidate all caches on a schema change
_MAX_FULL_LINES = 400         # get_entity(full) cap before it suggests skeleton
_SKELETON_MIN_LINES = 40      # below this, skeleton saves nothing -> return full
_FILE_SKELETON_MAX_LINES = 120  # above this, a file gets a graph outline, not a raw skeleton
_MAX_TRAVERSE_CHARS = 6000

_STATE: dict = {}


def _iter_source_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        for f in files:
            if f.endswith(SOURCE_EXTS) and not f.endswith('.d.ts'):
                yield os.path.join(root, f)


def _repo_signature() -> str:
    h = hashlib.sha256()
    rows = []
    for p in _iter_source_files():
        try:
            st = os.stat(p)
        except OSError:
            continue
        rel = os.path.relpath(p, REPO).replace(os.sep, '/')
        rows.append(f'{rel}|{int(st.st_mtime)}|{st.st_size}')
    for row in sorted(rows):
        h.update(row.encode('utf-8') + b'\n')
    h.update(_CACHE_SCHEMA.encode())
    return h.hexdigest()


def _ensure_loaded() -> None:
    if _STATE:
        return
    with _protect_stdout():
        _build_state()


def _build_state() -> None:
    sig = _repo_signature()
    gpkl = CACHE_DIR / 'graph.pkl'
    sigf = CACHE_DIR / 'signature'
    bm_dir = CACHE_DIR / 'bm25'

    graph = bm25 = None
    if gpkl.exists() and sigf.exists() and bm_dir.exists():
        try:
            if sigf.read_text(encoding='utf-8').strip() == sig:
                graph = pickle.loads(gpkl.read_bytes())
                bm25 = TsBM25Index.load(str(bm_dir))
        except Exception:  # noqa: BLE001 -- corrupt cache -> rebuild
            graph = bm25 = None

    if graph is None or bm25 is None:
        graph = build_ts_graph(str(REPO))
        bm25 = TsBM25Index.from_graph(graph)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            gpkl.write_bytes(pickle.dumps(graph))
            bm25.save(str(bm_dir))
            sigf.write_text(sig, encoding='utf-8')
        except OSError as exc:  # read-only repo, etc. -- run without a cache
            print(f'[locagent] cache disabled: {exc}', file=sys.stderr)

    _STATE['graph'] = graph
    _STATE['searcher'] = RepoEntitySearcher(graph)
    _STATE['bm25'] = bm25
    print(f'[locagent] {REPO}  ({graph.number_of_nodes()} nodes, '
          f'{graph.number_of_edges()} edges)', file=sys.stderr)


# ────────────────────────────────  helpers  ─────────────────────────────────

def _resolve_id(raw: str) -> Tuple[Optional[str], List[str]]:
    """Map a user-supplied id to a graph node. Tolerates a missing directory
    prefix ("toolbars.tsx:Foo"), a bare entity name ("Foo") and a bare dotted
    name ("Class.method"). Returns (nid, suggestions)."""
    g = _STATE['graph']
    raw = raw.strip().strip('"\'`').replace('\\', '/')
    if raw in g:
        return raw, []

    file_part, _, ent_part = raw.partition(':')
    cands = []
    for nid in g.nodes:
        n_file, _, n_ent = nid.partition(':')
        if ent_part:  # "<file?>:<entity>" shorthand
            file_ok = (n_file == file_part or n_file.endswith('/' + file_part)
                       or n_file.split('/')[-1] == file_part)
            ent_ok = (n_ent == ent_part or n_ent.split('.')[-1] == ent_part.split('.')[-1]
                      or n_ent.endswith('.' + ent_part))
            if file_ok and ent_ok:
                cands.append(nid)
        else:  # bare name / path fragment
            if (nid.endswith('/' + raw) or n_ent == raw
                    or n_ent.split('.')[-1] == raw or n_file.split('/')[-1] == raw):
                cands.append(nid)

    non_test = [c for c in cands if not is_test_file(c)]
    cands = sorted(set(non_test or cands), key=lambda c: (c.count('.'), len(c)))
    if len(cands) == 1:
        return cands[0], []
    # an exact full-id-shorthand hit (right file basename + exact entity) wins
    exact = [c for c in cands
             if c.partition(':')[2] == ent_part and c.partition(':')[0].split('/')[-1] == file_part]
    if len(exact) == 1:
        return exact[0], []
    return None, cands[:12]


def _loc(nid: str, nd: dict) -> str:
    f = nid.split(':')[0]
    if 'start_line' in nd:
        return f'{f}:{nd["start_line"]}-{nd.get("end_line", nd["start_line"])}'
    return f


def _wrap_lines(code: str, start_line: int) -> str:
    lines = code.split('\n')
    w = len(str(start_line + len(lines) - 1))
    return '\n'.join(f'{str(i + start_line).rjust(w)} | {ln}' for i, ln in enumerate(lines))


def _rrf(*ranked_lists: List[str], k: int = 60) -> List[str]:
    """Reciprocal-rank fusion of several ranked id lists."""
    score: dict = {}
    for lst in ranked_lists:
        for rank, nid in enumerate(lst):
            score[nid] = score.get(nid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=score.get, reverse=True)


# ─────────────────────────────────  server  ─────────────────────────────────

mcp = FastMCP(
    'locagent-ts',
    instructions=(
        'Graph-guided code localization for this repository. Start with '
        'search_code_entities to find relevant functions/classes/components, '
        'then get_entity for the exact code (skeleton first) and traverse to '
        'follow imports / calls / inheritance / JSX renders. Never read a whole '
        'large file -- request the entity.'
    ),
)


@mcp.tool()
def search_code_entities(query: str, max_results: int = 10, scope: str = 'all') -> str:
    """Find code entities (functions, classes, React components, files) relevant
    to a natural-language query, ranked by a fusion of BM25 and fuzzy name match.

    Args:
        query: what you are looking for, e.g. "selection toolbar styling".
        max_results: how many entities to return (default 10).
        scope: one of "all", "function", "class", "file".
    """
    _ensure_loaded()
    g = _STATE['graph']
    if scope not in ('all', NODE_TYPE_FUNCTION, NODE_TYPE_CLASS, NODE_TYPE_FILE):
        return f'bad scope {scope!r}; use one of all|function|class|file'
    max_results = max(1, min(max_results, 30))

    with _protect_stdout():
        bm = _STATE['bm25'].retrieve(query, k=max_results * 2, search_scope=scope)
        try:
            fz = fuzzy_retrieve_from_graph_nodes(
                query, graph=g, search_scope=scope, similarity_top_k=max_results * 2)
        except Exception:  # noqa: BLE001
            fz = []
    fused = _rrf(bm, fz)[:max_results]
    if not fused:
        return f'no matches for {query!r}'

    searcher = _STATE['searcher']
    out = [f'{len(fused)} entities for {query!r}:']
    for nid in fused:
        nd = g.nodes[nid]
        head = ''
        if nd.get('type') != NODE_TYPE_FILE:
            sk = (nd.get('skeleton') or '').splitlines()
            head = next((ln.strip() for ln in sk if ln.strip()
                         and not ln.strip().startswith(('//', '/*', '*'))), '')
        tag = ' [component]' if nd.get('is_component') else ''
        out.append(f'- {nid}{tag}  ({nd.get("type")}, {_loc(nid, nd)})'
                   + (f'\n    {head[:160]}' if head else ''))
    out.append('\nNext: get_entity("<id>") for code, traverse("<id>") for neighbours.')
    return '\n'.join(out)


@mcp.tool()
def get_entity(entity_id: str = '', mode: str = 'skeleton', id: str = '') -> str:
    """Return the source of one entity by its graph id (as printed by
    search_code_entities, e.g. "src/board/toolbars.tsx:ImageFormatToolbar").

    Args:
        entity_id: the node id (a bare name is resolved if unambiguous). `id` is
            accepted as an alias.
        mode: "skeleton" (signatures + JSDoc, bodies elided) or "full". Small
            entities are returned in full regardless -- eliding saves nothing.
    """
    _ensure_loaded()
    g = _STATE['graph']
    target = entity_id or id
    if not target:
        return 'provide "entity_id" -- a node id from search_code_entities'
    nid, sugg = _resolve_id(target)
    if nid is None:
        if sugg:
            return 'ambiguous / not found. did you mean:\n' + '\n'.join(f'  {s}' for s in sugg)
        return f'no entity {target!r} in this repo'

    nd = g.nodes[nid]
    ntype = nd.get('type')
    code = nd.get('code', '') or ''
    start_line = nd.get('start_line', 1)

    if ntype == NODE_TYPE_DIRECTORY:
        return f'{nid} is a directory; use get_repo_overview or traverse.'

    if mode == 'skeleton':
        if ntype == NODE_TYPE_FILE:
            n_lines = code.count('\n') + 1
            raw = get_skeleton(code, language=nd.get('language'))
            if raw.count('\n') + 1 <= _FILE_SKELETON_MAX_LINES:
                return f'{nid}  (file, {n_lines} lines)  [skeleton]\n```\n{raw}\n```'
            # big file: a raw skeleton of a 9k-line file is still ~1.4k lines.
            # give the compact structural outline from the graph instead --
            # top-level entities with line ranges + a hint to drill in.
            rows = []
            for _, child, ed in g.out_edges(nid, data=True):
                if ed.get('type') != 'contains':
                    continue
                cd = g.nodes[child]
                if cd.get('type') not in (NODE_TYPE_FUNCTION, NODE_TYPE_CLASS):
                    continue
                nm = child.split(':', 1)[1]
                if '.' in nm:            # nested -> skip, only top-level here
                    continue
                sig = next((ln.strip() for ln in (cd.get('skeleton') or '').splitlines()
                            if ln.strip() and not ln.strip().startswith(('//', '/*', '*'))), nm)
                rows.append((cd.get('start_line', 0),
                             f'  L{cd.get("start_line", "?")}-{cd.get("end_line", "?")}  '
                             f'{sig[:140]}{" [component]" if cd.get("is_component") else ""}'))
            if not rows:
                head = '\n'.join(raw.splitlines()[:_FILE_SKELETON_MAX_LINES])
                return (f'{nid}  (file, {n_lines} lines)  [skeleton head]\n```\n{head}\n'
                        f'... (large file, skeleton truncated)\n```')
            rows.sort()
            shown = [r for _, r in rows[:60]]
            more = f'\n  ... {len(rows) - 60} more' if len(rows) > 60 else ''
            return (f'{nid}  (file, {n_lines} lines, {len(rows)} top-level entities)  [outline]\n'
                    f'```\n' + '\n'.join(shown) + more + '\n```\n'
                    f'get_entity("{nid}:<Name>", "skeleton" | "full") for one.')
        n_lines = code.count('\n') + 1
        if n_lines <= _SKELETON_MIN_LINES:
            numbered = _wrap_lines(code, start_line)
            return (f'{nid}  ({ntype}, {_loc(nid, nd)})  [full: {n_lines} lines, '
                    f'skeleton would elide everything]\n```\n{numbered}\n```')
        body = nd.get('skeleton') or get_skeleton(code, language='tsx')
        return f'{nid}  ({ntype}, {_loc(nid, nd)})  [skeleton]\n```\n{body}\n```'

    # full
    n_lines = code.count('\n') + 1
    if n_lines > _MAX_FULL_LINES:
        sk = get_skeleton(code, language=nd.get('language') or 'tsx')
        return (f'{nid} is {n_lines} lines -- too large to inline. Skeleton below; '
                f'request a nested entity for a specific part.\n```\n{sk}\n```')
    numbered = _wrap_lines(code, start_line if ntype != NODE_TYPE_FILE else 1)
    return f'{nid}  ({ntype}, {_loc(nid, nd)})  [full]\n```\n{numbered}\n```'


@mcp.tool()
def traverse(entity_id: str = '', edge_types: Optional[List[str]] = None,
             direction: str = 'both', hops: int = 2, id: str = '',
             include_tests: Optional[bool] = None) -> str:
    """Show the neighbourhood of an entity in the code graph as an indented tree.

    Args:
        entity_id: node id to start from. `id` is accepted as an alias.
        edge_types: subset of ["contains","imports","invokes","inherits","renders"]
            (default: all).
        direction: "downstream" (this -> others), "upstream" (others -> this) or "both".
        hops: traversal depth, 1-4 (default 2).
        include_tests: show neighbours in test files. Defaults to True for
            "upstream" (you want to know which tests use X before a refactor)
            and False otherwise.

    For refactor impact ("who breaks if I change X's signature") use
    direction="upstream", edge_types=["invokes","imports"]: `invokes` gives the
    runtime callers, `imports` also catches dependents whose call sites are not
    in a named entity (e.g. assertions inside anonymous it()/test callbacks).
    """
    _ensure_loaded()
    g = _STATE['graph']
    target = entity_id or id
    if not target:
        return 'provide "entity_id" -- a node id from search_code_entities'
    nid, sugg = _resolve_id(target)
    if nid is None:
        return ('ambiguous / not found. candidates:\n' + '\n'.join(f'  {s}' for s in sugg)) \
            if sugg else f'no entity {target!r} in this repo'
    if direction not in ('downstream', 'upstream', 'both'):
        return 'direction must be downstream | upstream | both'
    hops = max(1, min(hops, 4))
    if edge_types:
        bad = [e for e in edge_types if e not in VALID_EDGE_TYPES]
        if bad:
            return f'unknown edge types {bad}; valid: {VALID_EDGE_TYPES}'
    if include_tests is None:
        include_tests = direction == 'upstream'

    tree = traverse_tree_structure(g, nid, direction=direction, hops=hops,
                                   edge_type_filter=edge_types,
                                   include_tests=include_tests)
    if not tree or tree.strip() == nid:
        return f'{nid} has no {"/".join(edge_types) if edge_types else ""} neighbours ' \
               f'in direction {direction} within {hops} hop(s).'
    if len(tree) > _MAX_TRAVERSE_CHARS:
        tree = tree[:_MAX_TRAVERSE_CHARS] + '\n... (truncated; narrow edge_types or hops)'
    return f'{direction} from {nid} ({hops} hop(s)):\n```\n{tree}\n```'


@mcp.tool()
def get_repo_overview(max_depth: int = 3) -> str:
    """A directory tree of the repo's source files plus entity counts. Use this
    first to learn the layout."""
    _ensure_loaded()
    g = _STATE['graph']

    children: dict = {}
    for u, v, d in g.edges(data=True):
        if d.get('type') == 'contains' and g.nodes[v].get('type') in (
                NODE_TYPE_DIRECTORY, NODE_TYPE_FILE):
            children.setdefault(u, []).append(v)

    lines: List[str] = []

    def walk(node: str, prefix: str, depth: int) -> None:
        kids = sorted(children.get(node, []),
                      key=lambda n: (g.nodes[n].get('type') != NODE_TYPE_DIRECTORY, n))
        for i, kid in enumerate(kids):
            last = i == len(kids) - 1
            kd = g.nodes[kid]
            label = kid.split('/')[-1]
            if kd.get('type') == NODE_TYPE_FILE:
                n_ent = sum(1 for _, w, e in g.out_edges(kid, data=True)
                            if e.get('type') == 'contains')
                label += f'  ({n_ent} entities)' if n_ent else ''
            lines.append(f'{prefix}{"└── " if last else "├── "}{label}')
            if kd.get('type') == NODE_TYPE_DIRECTORY and depth < max_depth:
                walk(kid, prefix + ('    ' if last else '│   '), depth + 1)

    walk('/', '', 1)

    n = {'file': 0, 'function': 0, 'class': 0, 'component': 0}
    for _, nd in g.nodes(data=True):
        t = nd.get('type')
        if t in n:
            n[t] += 1
        if nd.get('is_component'):
            n['component'] += 1
    e: dict = {}
    for _, _, d in g.edges(data=True):
        e[d.get('type')] = e.get(d.get('type'), 0) + 1

    header = (f'{REPO.name}: {n["file"]} files, {n["function"]} functions '
              f'({n["component"]} React components), {n["class"]} classes\n'
              f'edges: {e}\n')
    return header + '```\n' + '\n'.join(lines[:400]) + '\n```'


if __name__ == '__main__':
    sys.stdout = _REAL_STDOUT  # hand the real stdout to the JSON-RPC transport
    mcp.run(transport='stdio')
