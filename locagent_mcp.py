#!/usr/bin/env python
"""LocAgent-TS -- MCP server for graph-guided code localization over ONE repo.

The repo is the server's working directory (or ``$LOCAGENT_REPO``). On first use
it builds the tree-sitter code graph + a BM25 index for that repo and caches them
under ``<repo>/.locagent/`` (or ``$LOCAGENT_CACHE_DIR`` -- use this for a
read-only tree); subsequent starts reload the cache unless the set of source
files (path / mtime / size) changed. A repo it cannot write to just runs without
a cache.

Tools:
  graph_search(query, max_results=10, scope="all")
  graph_get(entity_id, mode="skeleton"|"full")
  graph_traverse(entity_id, edge_types=None, direction="both", hops=2)
  graph_map(max_depth=3)

Wire into Cline (`~/.cline/data/settings/cline_mcp_settings.json`):
  "locagent": {
    "command": "C:/Users/joz/orca/projects/locagent-ts/.venv/Scripts/python.exe",
    "args": ["C:/Users/joz/orca/projects/locagent-ts/locagent_mcp.py"],
    "cwd": "C:/path/to/the/target/worktree"
  }
"""

import contextlib
import json
import logging
import os
import pickle
import sys
import time
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
    NODE_TYPE_CONTEXT,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    VALID_EDGE_TYPES,
)
from dependency_graph.ts_bm25 import TsBM25Index  # noqa: E402
from dependency_graph.ts_build_graph import (  # noqa: E402
    build_ts_graph,
    iter_source_files,
    patch_ts_graph,
)
from dependency_graph.traverse_graph import (  # noqa: E402
    RepoEntitySearcher,
    _edge_annot,
    is_test_file,
    traverse_tree_structure,
)
from dependency_graph.ts_edit import edit_entity  # noqa: E402
from plugins.location_tools.retriever.fuzzy_retriever import (  # noqa: E402
    fuzzy_retrieve_from_graph_nodes,
)
from plugins.location_tools.utils.compress_file_ts import get_skeleton  # noqa: E402

# ─────────────────────────────  repo / cache  ────────────────────────────────

REPO = Path(os.environ.get('LOCAGENT_REPO', os.getcwd())).resolve()
# cache lives in the target repo by default; override for read-only trees.
CACHE_DIR = Path(os.environ['LOCAGENT_CACHE_DIR']).resolve() \
    if os.environ.get('LOCAGENT_CACHE_DIR') else REPO / '.locagent'
# Global bare-name matching for invokes/renders the import scope cannot resolve.
# Off by default: it is the setting that linked 294 753 production->test-double
# edges on a 3k-file repo. Opt in with LOCAGENT_FUZZY=1 for repos whose imports
# the resolver cannot follow (barrel-heavy layouts).
_FUZZY = os.environ.get('LOCAGENT_FUZZY', '').strip().lower() in ('1', 'true', 'yes')
# graph_edit mutates the repository. Evidence is not authority: the server stays
# read-only unless the operator opts in explicitly, so an agent that auto-discovers
# the tools cannot write to a live checkout by accident.
_ALLOW_EDITS = os.environ.get('LOCAGENT_ALLOW_EDITS', '').strip().lower() in ('1', 'true', 'yes')
_CACHE_SCHEMA = 'v6'          # bump to invalidate all caches on a schema change
                             # v2: invokes/renders edges carry call-site lines + JSX props
                             # v3: renders/invokes disambiguated by import binding
                             # v4: BM25 doc carries a weighted comment/JSDoc field
                             # v5: files with a stray non-UTF-8 byte are no longer
                             #     dropped from the graph (Board.tsx was missing)
                             # v6: import-scoped resolution by default (no global
                             #     name-match), context nodes + context edges,
                             #     string-aware JSONC tsconfig parsing
_MAX_FULL_LINES = 400         # graph_get(full) cap before it suggests skeleton
_SKELETON_MIN_LINES = 40      # below this, skeleton saves nothing -> return full
_FILE_SKELETON_MAX_LINES = 120  # above this, a file gets a graph outline, not a raw skeleton
_MAX_TRAVERSE_CHARS = 6000

_STATE: dict = {}


def _scan() -> dict:
    """``{rel_file: [mtime_ns, size]}`` for every source file the graph covers.

    Same walk as the builder (`iter_source_files`), so the scan and the graph can
    never disagree about what counts as a source file. Nanosecond mtime matters:
    an agent that edits a file and immediately asks a question must be seen, and
    second-granularity mtimes with an unchanged byte count would hide it."""
    out = {}
    for rel_file, abs_path, _grammar, _rel_dir in iter_source_files(str(REPO)):
        try:
            st = os.stat(abs_path)
        except OSError:
            continue
        out[rel_file] = [st.st_mtime_ns, st.st_size]
    return out


def _sig_payload(scan: dict) -> str:
    return json.dumps({'schema': _CACHE_SCHEMA, 'files': scan}, sort_keys=True)


def _ensure_loaded() -> None:
    if not _STATE:
        with _protect_stdout():
            _build_state()
    _refresh()


def _refresh() -> None:
    """Bring the in-memory graph up to date with the working tree.

    Runs before every tool call. `os.stat` on the source set is ~ms; the graph
    itself is only re-derived for the files that changed (plus the files that
    reference them), so an edit costs a patch, not a rebuild."""
    scan = _scan()
    old = _STATE.get('scan') or {}
    if scan == old:
        return
    changed = [rel for rel, v in scan.items() if old.get(rel) != v]
    deleted = [rel for rel in old if rel not in scan]
    g = _STATE['graph']
    t0 = time.time()
    try:
        if _FUZZY and (changed or deleted):
            raise RuntimeError('global name matching is not patchable')
        with _protect_stdout():
            stats = patch_ts_graph(g, str(REPO), changed + deleted)
        _STATE['searcher'] = RepoEntitySearcher(g)
        _STATE['dirty_bm25'] = True
        print(f'[locagent] patched {len(changed)} changed / {len(deleted)} deleted '
              f'file(s) in {stats["seconds"]*1000:.0f}ms -> '
              f'{g.number_of_nodes()} nodes, {g.number_of_edges()} edges',
              file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 -- anything unexpected: full rebuild
        print(f'[locagent] incremental patch failed ({exc}); rebuilding',
              file=sys.stderr)
        with _protect_stdout():
            g = build_ts_graph(str(REPO), fuzzy_search=_FUZZY)
            _STATE['graph'] = g
            _STATE['searcher'] = RepoEntitySearcher(g)
            _STATE['bm25'] = TsBM25Index.from_graph(g)
            _STATE['dirty_bm25'] = False
        print(f'[locagent] rebuilt in {time.time() - t0:.2f}s', file=sys.stderr)
    _STATE['scan'] = scan
    # the on-disk cache is now behind the working tree: leave it stale so the
    # next start rebuilds instead of loading a graph that disagrees with the repo
    if CACHE_DIR.is_dir():
        try:
            (CACHE_DIR / 'signature').write_text(
                json.dumps({'schema': _CACHE_SCHEMA, 'files': {}}), encoding='utf-8')
        except OSError:
            pass


def _bm25() -> TsBM25Index:
    """The BM25 index, rebuilt from the current graph if edits invalidated it.
    Kept out of the edit path (`graph_get` / `graph_traverse` never need it) and
    paid on the next search instead."""
    if _STATE.get('dirty_bm25') or 'bm25' not in _STATE:
        with _protect_stdout():
            _STATE['bm25'] = TsBM25Index.from_graph(_STATE['graph'])
        _STATE['dirty_bm25'] = False
    return _STATE['bm25']


def _build_state() -> None:
    scan = _scan()
    gpkl = CACHE_DIR / 'graph.pkl'
    sigf = CACHE_DIR / 'signature'
    bm_dir = CACHE_DIR / 'bm25'

    graph = bm25 = None
    if gpkl.exists() and sigf.exists() and bm_dir.exists():
        try:
            stored = json.loads(sigf.read_text(encoding='utf-8'))
            if stored.get('schema') == _CACHE_SCHEMA and stored.get('files') == scan:
                graph = pickle.loads(gpkl.read_bytes())
                bm25 = TsBM25Index.load(str(bm_dir))
        except Exception:  # noqa: BLE001 -- corrupt cache -> rebuild
            graph = bm25 = None

    if graph is None or bm25 is None:
        graph = build_ts_graph(str(REPO), fuzzy_search=_FUZZY)
        bm25 = TsBM25Index.from_graph(graph)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            gpkl.write_bytes(pickle.dumps(graph))
            bm25.save(str(bm_dir))
            sigf.write_text(_sig_payload(scan), encoding='utf-8')
        except OSError as exc:  # read-only repo, etc. -- run without a cache
            print(f'[locagent] cache disabled: {exc}', file=sys.stderr)

    _STATE['graph'] = graph
    _STATE['searcher'] = RepoEntitySearcher(graph)
    _STATE['bm25'] = bm25
    _STATE['scan'] = scan
    _STATE['dirty_bm25'] = False
    print(f'[locagent] {REPO}  ({graph.number_of_nodes()} nodes, '
          f'{graph.number_of_edges()} edges)', file=sys.stderr)


# ────────────────────────────────  helpers  ─────────────────────────────────

def _resolve_id(raw: str) -> Tuple[Optional[str], List[str]]:
    """Map a user-supplied id to a graph node. Tolerates a missing directory
    prefix ("toolbars.tsx:Foo"), a bare entity name ("Foo") and a bare dotted
    name ("Class.method"). Returns (nid, suggestions)."""
    g = _STATE['graph']
    raw = raw.strip().strip('"\'`').replace('\\', '/')
    # tolerate a trailing display tag copied from search output ("…:Foo [component]")
    while raw.endswith(']') and ' [' in raw:
        raw = raw[:raw.rindex(' [')].rstrip()
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


def _file_outline(g, nid: str, n_lines: int, raw_skeleton: str) -> str:
    """Compact structural view of a large file: its top-level entities from the
    graph's `contains` edges, one signature line each. A raw skeleton of a
    7k-line file is still ~1.4k lines; this is ~60."""
    rows = []
    for _, child, ed in g.out_edges(nid, data=True):
        if ed.get('type') != 'contains':
            continue
        cd = g.nodes[child]
        if cd.get('type') not in (NODE_TYPE_FUNCTION, NODE_TYPE_CLASS, NODE_TYPE_CONTEXT):
            continue
        nm = child.split(':', 1)[1]
        if '.' in nm:            # nested -> only top-level here
            continue
        sig = next((ln.strip() for ln in (cd.get('skeleton') or '').splitlines()
                    if ln.strip() and not ln.strip().startswith(('//', '/*', '*'))), nm)
        tag = ' [component]' if cd.get('is_component') else \
              ' [context]' if cd.get('type') == NODE_TYPE_CONTEXT else \
              f" [{cd['ts_kind']}]" if cd.get('ts_kind') else ''
        rows.append((cd.get('start_line', 0),
                     f'  L{cd.get("start_line", "?")}-{cd.get("end_line", "?")}  '
                     f'{sig[:140]}{tag}'))
    if not rows:
        head = '\n'.join(raw_skeleton.splitlines()[:_FILE_SKELETON_MAX_LINES])
        return (f'{nid}  (file, {n_lines} lines)  [skeleton head]\n```\n{head}\n'
                f'... (large file, truncated)\n```')
    rows.sort()
    shown = [r for _, r in rows[:60]]
    more = f'\n  ... {len(rows) - 60} more' if len(rows) > 60 else ''
    return (f'{nid}  (file, {n_lines} lines, {len(rows)} top-level entities)  [outline]\n'
            f'```\n' + '\n'.join(shown) + more + '\n```\n'
            f'graph_get("{nid}:<Name>", "skeleton" | "full") for one.')


def _missing_hint(g, raw: str) -> str:
    """Message for an id that resolves to nothing. If the caller named a file,
    answer with that file's entities instead of a bare "not found": the whole
    point of an entity-addressed action space is that a wrong name is correctable
    from the error."""
    raw = raw.strip().strip('"\'`').replace('\\', '/')
    file_part = raw.partition(':')[0]
    nid, _sugg = _resolve_id(file_part) if file_part else (None, [])
    if nid and g.nodes[nid].get('type') == NODE_TYPE_FILE:
        rows = []
        for _, v, ed in g.out_edges(nid, data=True):
            if ed.get('type') != 'contains' or ':' not in v:
                continue
            name = v.split(':', 1)[1]
            if '.' in name:            # nested entities -> via graph_get outline
                continue
            nd = g.nodes[v]
            rows.append(f'  {nid}:{name}  ({nd.get("type")}, '
                        f'L{nd.get("start_line")}-{nd.get("end_line")})')
        if rows:
            return (f'no entity {raw!r} in this repo, but {nid} defines:\n'
                    + '\n'.join(rows[:20])
                    + f'\n(use graph_get("{nid}") for the full outline, nested entities included)')
    return f'no entity {raw!r} in this repo'


def _resolve_or_hint(raw: str):
    """``(nid, message)`` -- exactly one of the two is set."""
    nid, sugg = _resolve_id(raw)
    if nid is not None:
        return nid, ''
    if sugg:
        return None, 'ambiguous / not found. did you mean:\n' + '\n'.join(f'  {s}' for s in sugg)
    return None, _missing_hint(_STATE['graph'], raw)


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
        'Graph-guided code localization for this repository. For ANY question of '
        'the form where is X / which calls or renders X / who uses X / what is '
        'prop P wired to / what breaks if X changes: use ONLY these graph tools. '
        'Do NOT use the native search_codebase / search_files / read_file / grep '
        'tools for such a question -- they are the wrong instrument and waste the '
        'context window; graph_search + graph_traverse already hold the answer.\n'
        'Order: graph_search (plain-language) to get the entity id -> graph_get '
        '(skeleton first) for its code -> graph_traverse to follow imports / '
        'calls / inheritance / JSX renders. Never read a whole large file.\n'
        'To ENUMERATE every place that calls / wraps / mounts an entity X '
        '(e.g. "all wrappers of reorder", "who calls applyZOrder", "everything '
        'that renders LayerButtons"), ONE call answers it: graph_traverse(X, '
        'direction="upstream", edge_types=["invokes"] or ["renders"]) -- it '
        'returns the complete list with call-site lines. Never assemble that '
        'list by hand with grep or reworded graph_search queries. Once that '
        'traverse has returned, the list IS complete -- synthesise your answer '
        'from it and stop; do not keep searching for more.\n'
        'One thing the graph does NOT model: a convention expressed as a runtime '
        'value check (e.g. "a note with color === undefined IS a text box", '
        '"keep this list in sync with X"). No edge carries it. Use the graph to '
        'find the cluster of entities involved, then grep/read is the right tool '
        'to close out those value-level conventions -- that hand-off is expected, '
        'not a failure.'
    ),
)


@mcp.tool()
def graph_search(query: str, max_results: int = 10, scope: str = 'all') -> str:
    """Find code entities (functions, classes, React components, React contexts,
    files) relevant to a natural-language query, ranked by a fusion of BM25 and
    fuzzy name match.

    Args:
        query: what you are looking for, e.g. "selection toolbar styling".
        max_results: how many entities to return (default 10).
        scope: one of "all", "function", "class", "context", "file".
    """
    _ensure_loaded()
    g = _STATE['graph']
    if scope not in ('all', NODE_TYPE_FUNCTION, NODE_TYPE_CLASS, NODE_TYPE_FILE,
                     NODE_TYPE_CONTEXT):
        return f'bad scope {scope!r}; use one of all|function|class|context|file'
    max_results = max(1, min(max_results, 30))

    with _protect_stdout():
        bm = _bm25().retrieve(query, k=max_results * 3, search_scope=scope)
        try:
            fz = fuzzy_retrieve_from_graph_nodes(
                query, graph=g, search_scope=scope, similarity_top_k=max_results * 3)
        except Exception:  # noqa: BLE001
            fz = []
    ranked = _rrf(bm, fz)
    if not ranked:
        return f'no matches for {query!r}'

    # an entity whose bare name is a query token is almost certainly the target;
    # BM25 otherwise buries it under tiny same-file helpers ("AiChatPanel" ->
    # SendIcon/CloseIcon ranked above the AiChatPanel component itself).
    qtokens = {t.lower().strip('"\'`(){}[]') for t in query.split()}

    def _bare(nid: str) -> str:
        return nid.split(':')[-1].split('.')[-1].lower() if ':' in nid \
            else nid.split('/')[-1].lower()

    exact = [n for n in ranked if _bare(n) in qtokens]
    exact.sort(key=lambda n: not g.nodes[n].get('is_component'))  # components first
    seen = set(exact)
    fused = (exact + [n for n in ranked if n not in seen])[:max_results]

    searcher = _STATE['searcher']
    out = [f'{len(fused)} entities for {query!r}:']
    for nid in fused:
        nd = g.nodes[nid]
        head = ''
        if nd.get('type') != NODE_TYPE_FILE:
            sk = (nd.get('skeleton') or '').splitlines()
            head = next((ln.strip() for ln in sk if ln.strip()
                         and not ln.strip().startswith(('//', '/*', '*'))), '')
        tag = ', component' if nd.get('is_component') else \
              ', context' if nd.get('type') == NODE_TYPE_CONTEXT else \
              f", {nd['ts_kind']}" if nd.get('ts_kind') else ''
        out.append(f'- {nid}  ({nd.get("type")}{tag}, {_loc(nid, nd)})'
                   + (f'\n    {head[:160]}' if head else ''))
    out.append('\nNext: graph_get("<id>") for code, graph_traverse("<id>") for neighbours.')
    return '\n'.join(out)


@mcp.tool()
def graph_get(entity_id: str = '', mode: str = 'skeleton', id: str = '') -> str:
    """Return the source of one entity by its graph id (as printed by
    graph_search, e.g. "src/board/toolbars.tsx:ImageFormatToolbar").

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
        return 'provide "entity_id" -- a node id from graph_search'
    nid, problem = _resolve_or_hint(target)
    if nid is None:
        return problem

    nd = g.nodes[nid]
    ntype = nd.get('type')
    code = nd.get('code', '') or ''
    start_line = nd.get('start_line', 1)

    if ntype == NODE_TYPE_DIRECTORY:
        return f'{nid} is a directory; use graph_map or graph_traverse.'

    n_lines = code.count('\n') + 1

    # FILE nodes: never inline a whole file. Both modes -> a compact raw
    # skeleton for a small file, else the graph outline of its entities.
    if ntype == NODE_TYPE_FILE:
        raw = get_skeleton(code, language=nd.get('language'))
        if raw.count('\n') + 1 <= _FILE_SKELETON_MAX_LINES:
            return f'{nid}  (file, {n_lines} lines)  [skeleton]\n```\n{raw}\n```'
        return _file_outline(g, nid, n_lines, raw)

    # ENTITY nodes
    if mode == 'skeleton':
        if n_lines <= _SKELETON_MIN_LINES:
            numbered = _wrap_lines(code, start_line)
            return (f'{nid}  ({ntype}, {_loc(nid, nd)})  [full: {n_lines} lines, '
                    f'skeleton would elide everything]\n```\n{numbered}\n```')
        body = nd.get('skeleton') or get_skeleton(code, language='tsx')
        return f'{nid}  ({ntype}, {_loc(nid, nd)})  [skeleton]\n```\n{body}\n```'

    # full
    if n_lines > _MAX_FULL_LINES:
        body = nd.get('skeleton') or get_skeleton(code, language=nd.get('language') or 'tsx')
        return (f'{nid} is {n_lines} lines -- too large to inline. Skeleton (bodies '
                f'elided); request a nested entity for a specific part.\n```\n{body}\n```')
    numbered = _wrap_lines(code, start_line)
    return f'{nid}  ({ntype}, {_loc(nid, nd)})  [full]\n```\n{numbered}\n```'


@mcp.tool()
def graph_traverse(entity_id: str = '', edge_types: Optional[List[str]] = None,
                   direction: str = 'both', hops: int = 2, id: str = '',
                   include_tests: Optional[bool] = None) -> str:
    """Show the neighbourhood of an entity in the code graph as an indented tree.

    Args:
        entity_id: node id to start from. `id` is accepted as an alias.
        edge_types: subset of ["contains","imports","invokes","inherits","renders",
            "consumes_context","provides_context"] (default: all).
        direction: "downstream" (this -> others), "upstream" (others -> this) or "both".
        hops: traversal depth, 1-4 (default 2). For "what directly renders / calls
            X" use hops=1 -- the answer is the first level. hops>=2 also shows the
            grandparent; the `@L<line>` on a deeper edge is where THAT parent is
            mounted/called, NOT where X is. Never attribute a grandparent's line
            to X.
        include_tests: show neighbours in test files. Defaults to True for
            "upstream" (you want to know which tests use X before a refactor)
            and False otherwise.

    To list EVERY caller / wrapper / mount site of X in one call, use
    direction="upstream" with edge_types=["invokes"] (callers and wrappers) or
    ["renders"] (JSX mount sites). The result is the complete set with
    `@L<line>` call sites -- do not fall back to grep or to re-worded
    graph_search queries to assemble that list by hand.

    For refactor impact ("who breaks if I change X's signature") use
    direction="upstream", edge_types=["invokes","imports"]: `invokes` gives the
    runtime callers, `imports` also catches dependents whose call sites are not
    in a named entity (e.g. assertions inside anonymous it()/test callbacks).

    `invokes` and `renders` edges are annotated with the call site:
    `... invokes ── foo  @L120,204` (called on lines 120 and 204) and
    `... renders ── Toolbar  @L88 {onAction=handleAiAction, onClose=close}`
    (mounted at line 88 with those props wired) -- so you can jump straight to
    the wiring without grepping the file. Each `@L<line>` belongs to the edge it
    sits on: it is a line in the PARENT (the renderer / caller), pointing at
    where that parent mounts or calls its child.
    """
    _ensure_loaded()
    g = _STATE['graph']
    target = entity_id or id
    if not target:
        return 'provide "entity_id" -- a node id from graph_search'
    nid, problem = _resolve_or_hint(target)
    if nid is None:
        return problem
    if direction not in ('downstream', 'upstream', 'both'):
        return 'direction must be downstream | upstream | both'
    hops = max(1, min(hops, 4))
    if edge_types:
        bad = [e for e in edge_types if e not in VALID_EDGE_TYPES]
        if bad:
            return f'unknown edge types {bad}; valid: {VALID_EDGE_TYPES}'
    if include_tests is None:
        include_tests = direction == 'upstream'

    et_label = '/'.join(edge_types) if edge_types else ''

    # A file id has no invokes/renders/inherits edges -- those attach to its
    # functions/classes. Expand to the file's top-level entities and graph_traverse
    # from each, so `graph_traverse("foo.tsx", edge_types=["renders"])` still answers.
    if g.nodes[nid].get('type') == NODE_TYPE_FILE:
        kids = [v for _, v, ed in g.out_edges(nid, data=True)
                if ed.get('type') == 'contains'
                and g.nodes[v].get('type') in (NODE_TYPE_FUNCTION, NODE_TYPE_CLASS,
                                               NODE_TYPE_CONTEXT)
                and '.' not in v.split(':', 1)[1]]
        blocks = []
        for k in sorted(kids):
            t = traverse_tree_structure(g, k, direction=direction, hops=hops,
                                        edge_type_filter=edge_types,
                                        include_tests=include_tests)
            if t and t.strip() != k:
                blocks.append(t)
        if not blocks:
            return (f'{nid} ({len(kids)} entities) has no {et_label} neighbours '
                    f'in direction {direction} within {hops} hop(s). '
                    f'(renders/invokes/inherits attach to entities, not files.)')
        out = '\n\n'.join(blocks)
        if len(out) > _MAX_TRAVERSE_CHARS:
            out = out[:_MAX_TRAVERSE_CHARS] + '\n... (truncated; graph_traverse one entity)'
        return (f'{direction} from the entities of {nid} ({hops} hop(s)):\n'
                f'```\n{out}\n```')

    tree = traverse_tree_structure(g, nid, direction=direction, hops=hops,
                                   edge_type_filter=edge_types,
                                   include_tests=include_tests)
    if not tree or tree.strip() == nid:
        return f'{nid} has no {et_label} neighbours ' \
               f'in direction {direction} within {hops} hop(s).'
    if len(tree) > _MAX_TRAVERSE_CHARS:
        tree = tree[:_MAX_TRAVERSE_CHARS] + '\n... (truncated; narrow edge_types or hops)'
    return f'{direction} from {nid} ({hops} hop(s)):\n```\n{tree}\n```'


@mcp.tool()
def graph_edit(entity_id: str = '', operation: str = 'replace_in_node',
               replacement: str = '', old_str: str = '', new_str: str = '',
               id: str = '', dry_run: bool = False,
               position: str = 'end',
               edits: Optional[List[dict]] = None) -> str:
    """Edit ONE entity in the working tree, addressed by its graph id, or a batch of them.

    Args:
        entity_id: graph id of the entity to change, as printed by graph_search
            (e.g. "src/board/toolbars.tsx:ImageFormatToolbar"). `id` is an alias.
        operation: one of "replace_in_node" (default; one unique substring inside
            the entity), "insert_member" (a new member inside the entity's body --
            an interface field, a class method, a statement), "replace_node" (the
            whole entity), "insert_before", "insert_after", "delete_node".
        replacement: the new code, for replace_node / insert_*.
        old_str, new_str: the substring and its replacement, for replace_in_node.
        dry_run: report what would change without writing.
        position: for "insert_member", "end" (default; just before the closing
            brace) or "start" (top of the body). Use "start" when the body already
            references the new member above its insertion point: the syntax gate
            cannot see "used before declaration" -- that is tsc's job -- so the
            placement has to be sayable.
        edits: a batch -- a list of {"entity_id", "operation", "replacement" |
            "old_str"/"new_str"} objects applied in order in ONE call. A step that
            changes several entities should be one call, not one call per edit: the
            cost of this harness is round-trips, and each turn re-sends the context.
            The batch stops at the first refusal and reports which edits were
            already written, so a partial application is never silent. Use separate
            calls only when the next edit depends on seeing the previous result.

    Prefer "replace_in_node" for anything small: replacing a whole entity rewrites
    code you did not read and the diff stops being reviewable.

    The write is REFUSED when it would make the file parse worse (tree-sitter
    ERROR nodes, own grammar per extension) or when the substring is not unique --
    the file is left untouched. After a successful edit the graph is refreshed and
    the report lists the entities that reference what you changed, so you can fix
    the wiring that the edit just orphaned: `invokes` (callers), `renders` (mount
    sites), `consumes_context` / `provides_context`.

    Edits are disabled unless the server was started with LOCAGENT_ALLOW_EDITS=1:
    evidence is not authority, and this tool mutates the repository.
    """
    if edits is not None:
        return _graph_edit_batch(edits, dry_run=dry_run)
    return _edit_one(entity_id=entity_id, operation=operation, replacement=replacement,
                     old_str=old_str, new_str=new_str, id=id, dry_run=dry_run,
                     position=position)[1]


def _graph_edit_batch(edits: List[dict], dry_run: bool = False) -> str:
    """Apply several entity edits in one round-trip.

    Sequential and fail-fast: each edit is validated against the file as the
    previous one left it (the entity is re-resolved by name on a fresh parse, so
    line numbers cannot drift), and the first refusal stops the batch -- reporting
    what was already written, because a partial application the caller cannot see
    is worse than a failed one.

    The reason this exists: measured on the opacity task, one edit per turn cost
    417k input tokens against 58-63k for the text editor's ~1.8 edits per turn.
    The expense was round-trips, not per-turn context, so the fix is to make N
    edits fit in one call.
    """
    if not isinstance(edits, list) or not edits:
        return 'edits tiene que ser una lista no vacía de operaciones'
    written: List[str] = []
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            return f'edición {index}: se esperaba un objeto, llegó {type(edit).__name__}'
        target = edit.get('entity_id') or edit.get('id') or ''
        operation = edit.get('operation') or 'replace_in_node'
        single = {k: v for k, v in edit.items() if k in
                  ('replacement', 'old_str', 'new_str', 'dry_run', 'position')}
        ok, out = _edit_one(entity_id=target, operation=operation, **single)
        head = out.split('\n', 1)[0]
        if not ok:
            return ('lote detenido en la edición ' + str(index) + ' de ' + str(len(edits)) + ':\n'
                    + out + ('\nYa escritas:\n  ' + '\n  '.join(written) if written
                             else '\nNada se escribió.'))
        written.append(f'{index}. {head}')
    head = f'{len(written)} edición(es) en un lote' + (' [dry_run]' if dry_run else '')
    return head + '\n' + '\n'.join(written) + '\n' + _batch_references(edits)


def _batch_references(edits: List[dict]) -> str:
    """Union of the references touched by a batch, so the caller sees the wiring
    the whole step left misaligned instead of one entity's worth per call."""
    g = _STATE.get('graph')
    if g is None:
        return ''
    seen: List[Tuple[str, str, str]] = []
    for edit in edits:
        nid, _problem = _resolve_or_hint(edit.get('entity_id') or edit.get('id') or '')
        if nid is None:
            continue
        for ref in _references_to(g, nid):
            if ref not in seen:
                seen.append(ref)
    if not seen:
        return ''
    rows = ['referencias a revisar:'] + [f'  {e} ── {s}  {a}'.rstrip() for s, e, a in seen[:25]]
    if len(seen) > 25:
        rows.append(f'  ... {len(seen) - 25} más (graph_traverse para el resto)')
    return '\n'.join(rows)


def _edit_one(entity_id: str = '', operation: str = 'replace_in_node',
             replacement: str = '', old_str: str = '', new_str: str = '',
             id: str = '', dry_run: bool = False,
             position: str = 'end') -> Tuple[bool, str]:
    _ensure_loaded()
    if not _ALLOW_EDITS:
        return False, ('edición deshabilitada: este servidor es de sólo lectura. '
                       'Reiniciá con LOCAGENT_ALLOW_EDITS=1 para habilitar graph_edit.')
    g = _STATE['graph']
    target = entity_id or id
    if not target:
        return False, 'provide "entity_id" -- a node id from graph_search'
    nid, problem = _resolve_or_hint(target)
    if nid is None:
        return False, problem
    if ':' not in nid:
        kids = [v.split(':', 1)[1] for _, v, ed in g.out_edges(nid, data=True)
                if ed.get('type') == 'contains' and ':' in v]
        return False, (f'{nid} is a file, not an entity; edit one of its entities:\n'
                       + '\n'.join(f'  {nid}:{k}' for k in sorted(kids)[:20]))

    rel_file, name_path = nid.split(':', 1)
    before = _references_to(g, nid)

    with _protect_stdout():
        result = edit_entity(str(REPO), rel_file, name_path, operation,
                             replacement=replacement, old_str=old_str, new_str=new_str,
                             dry_run=dry_run, position=position)
    if result['status'] != 'ok':
        out = [f'no se editó nada: {result["message"]}']
        cands = result.get('candidates') or []
        if cands:
            out.append('entidades en el archivo:')
            out += [f'  - {rel_file}:{c["name"]}  ({c["type"]}, L{c["start_line"]}-{c["end_line"]})'
                    for c in cands[:20]]
        return False, '\n'.join(out)

    head = (f'{nid}  [{operation}]  {result["detail"]}  '
            f'(sintaxis: {result["syntax_errors_after"]} errores)'
            + ('  [dry_run: nada escrito]' if dry_run else ''))
    if dry_run:
        return True, head

    _refresh()                      # line ranges and edges now describe the new file
    g = _STATE['graph']
    out = [head]
    if result['entity_gone']:
        out.append(f'⚠ {name_path} ya no está definido en {rel_file}. '
                   'Los sitios que lo referenciaban quedaron huérfanos:')
    after_nid = f'{rel_file}:{name_path}'
    affected = before if result['entity_gone'] else _references_to(g, after_nid)
    if affected:
        label = 'referencias (antes de la edición)' if result['entity_gone'] else 'referencias actuales'
        out.append(f'{label} -- revisá el cableado:')
        out += [f'  {etype} ── {src}  {annot}' for src, etype, annot in affected[:25]]
        if len(affected) > 25:
            out.append(f'  ... {len(affected) - 25} más (graph_traverse para el resto)')
    else:
        out.append('ninguna entidad referencia a esto (nada que recablear).')
    return True, '\n'.join(out)


def describe_entities(ids: List[str]) -> List[dict]:
    """Address cards for programmatic callers (a planner, an eval harness).

    Not a tool: this is the addressing half of the structured action space, exposed
    so a caller can hand an agent the exact ids it is allowed to edit. Each card
    says whether the entity exists, what it is, its line range, whether it has a
    member body an ``insert_member`` could target, and -- when it does not exist --
    what the file does define, so the caller never has to guess.
    """
    _ensure_loaded()
    g = _STATE['graph']
    cards = []
    for raw in ids:
        nid, problem = _resolve_or_hint(raw)
        if nid is None or ':' not in nid:
            cards.append({'id': raw, 'found': False, 'hint': problem})
            continue
        nd = g.nodes[nid]
        cards.append({'id': nid, 'found': True, 'type': nd.get('type'),
                      'ts_kind': nd.get('ts_kind'),
                      'is_component': bool(nd.get('is_component')),
                      'line_start': nd.get('start_line'), 'line_end': nd.get('end_line'),
                      'member_body': nd.get('member_body'),
                      'signature': (nd.get('skeleton') or '').splitlines()[0][:160]
                      if nd.get('skeleton') else None})
    return cards


def _references_to(g, nid: str) -> List[Tuple[str, str, str]]:
    """Entities pointing at *nid*, with their edge type and site annotation."""
    out = []
    for src, _dst, ed in g.in_edges(nid, data=True):
        etype = ed.get('type')
        if etype in ('invokes', 'renders', 'consumes_context', 'provides_context',
                     'inherits'):
            out.append((src, etype, _edge_annot(ed).strip()))
    return sorted(out)


@mcp.tool()
def graph_map(max_depth: int = 3) -> str:
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

    n = {'file': 0, 'function': 0, 'class': 0, 'context': 0, 'component': 0}
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
              f'({n["component"]} React components), {n["class"]} classes, '
              f'{n["context"]} contexts\n'
              f'edges: {e}\n')
    return header + '```\n' + '\n'.join(lines[:400]) + '\n```'


if __name__ == '__main__':
    sys.stdout = _REAL_STDOUT  # hand the real stdout to the JSON-RPC transport
    mcp.run(transport='stdio')
