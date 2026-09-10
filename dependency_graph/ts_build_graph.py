"""tree-sitter graph builder for TypeScript / TSX / JS / JSX.

Parallel front end to ``build_graph.build_graph`` (which uses the Python ``ast``).
It produces the same heterogeneous ``networkx.MultiDiGraph`` -- same node-id
scheme (``dir`` / ``relpath`` / ``relpath:Dotted.Name``), same node types
(directory / file / class / function) and the same edge types
(contains / imports / inherits / invokes), plus one extra edge type
``renders`` (a component rendering another component through a JSX tag -- the UI
analogue of ``invokes``).

``invokes`` and ``renders`` edges also carry the call site: ``call_lines``
(list[int]) on ``invokes``; ``jsx_lines`` (list[int]) and ``sites``
(list of ``{line, props}`` where ``props`` is prop-name -> bound-expression
source, e.g. ``onAction`` -> ``handleAiAction``) on ``renders``. This closes the
v1 gap where the graph knew A renders B but not where, or which handlers were
wired to it.

Everything downstream of the graph (traverse_graph, BM25, the MCP tools) is
language-agnostic and consumes this graph unchanged.

CLI::

    python -m dependency_graph.ts_build_graph --repo /path/to/repo [--output graph.pkl]

Limitations (see docs/TYPESCRIPT_PORT_PLAN.md):
  * ``invokes`` / ``renders`` / ``inherits`` are matched by *name* (no type
    information), disambiguated by the caller's import binding then by file
    scope; a global name fallback still fires when nothing is in scope.
  * bare specifiers (``react``, ``konva``, ...) create no ``imports`` edge.
  * barrels / re-exports are resolved one hop only.
  * components referenced only through a value (``const I = {a: AIcon}; <I.a/>``)
    get no ``renders`` edge.
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
import time
from collections import defaultdict
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import networkx as nx
from tree_sitter_languages import get_language, get_parser

from dependency_graph.build_graph import (
    EDGE_TYPE_CONTAINS,
    EDGE_TYPE_IMPORTS,
    EDGE_TYPE_INHERITS,
    EDGE_TYPE_INVOKES,
    EDGE_TYPE_RENDERS,
    NODE_TYPE_CLASS,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    is_skip_dir,
)
from dependency_graph.ts_resolver import load_alias_map, resolve_import

# ─────────────────────────────  configuration  ────────────────────────────────

# extension -> tree-sitter grammar name.  .js/.jsx use the tsx grammar (superset,
# tolerates plain JS) so a mixed repo still gets a graph.
GRAMMAR_BY_EXT: Dict[str, str] = {
    '.ts': 'typescript',
    '.mts': 'typescript',
    '.cts': 'typescript',
    '.tsx': 'tsx',
    '.js': 'tsx',
    '.jsx': 'tsx',
    '.mjs': 'tsx',
    '.cjs': 'tsx',
}
SOURCE_EXTS = tuple(GRAMMAR_BY_EXT)

# directories never worth walking for application code.
SKIP_DIRS = {
    'node_modules', '.git', '.github', '.hg', '.svn', 'dist', 'build', 'out',
    'coverage', '.next', '.nuxt', '.turbo', '.cache', '.vite', '__snapshots__',
    'vendor', '.yarn', '.pnp',
}

_QUERY_DIR = os.path.join(os.path.dirname(__file__), 'queries')

# def-capture name -> graph node type
_DEF_KIND = {
    'def.function': NODE_TYPE_FUNCTION,
    'def.class': NODE_TYPE_CLASS,
    'def.wrapped': NODE_TYPE_FUNCTION,
}

# extremely common Array / Promise / string / DOM method names -- a bare-name
# `invokes` match on these is almost always noise (no type info to disambiguate).
_CALL_STOPLIST = frozenset({
    'map', 'filter', 'forEach', 'reduce', 'find', 'findIndex', 'some', 'every',
    'push', 'pop', 'shift', 'unshift', 'slice', 'splice', 'concat', 'join',
    'sort', 'reverse', 'includes', 'indexOf', 'flat', 'flatMap', 'fill', 'keys',
    'values', 'entries', 'then', 'catch', 'finally', 'bind', 'call', 'apply',
    'toString', 'valueOf', 'hasOwnProperty', 'trim', 'split', 'replace',
    'replaceAll', 'padStart', 'padEnd', 'toLowerCase', 'toUpperCase', 'startsWith',
    'endsWith', 'match', 'matchAll', 'test', 'exec', 'assign', 'freeze', 'stringify',
    'parse', 'now', 'random', 'floor', 'ceil', 'round', 'abs', 'min', 'max',
    'addEventListener', 'removeEventListener', 'setAttribute', 'getAttribute',
    'querySelector', 'querySelectorAll', 'getElementById', 'appendChild',
    'removeChild', 'preventDefault', 'stopPropagation', 'setState', 'log', 'warn',
    'error', 'info', 'assert', 'from', 'of', 'isArray', 'setTimeout',
    'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame',
})

_JSX_ELEMENT_TYPES = {'jsx_element', 'jsx_self_closing_element', 'jsx_fragment'}
_CLASS_NODE_TYPES = {'class_declaration', 'abstract_class_declaration', 'class'}
_SKELETON_MAX = 600
# max number of enclosing captured defs before a nested def is dropped as noise
# (e.g. a named helper inside a `.map()` callback inside a method inside a class).
_MAX_NEST_DEPTH = 3


# ──────────────────────────────  query loading  ──────────────────────────────

@lru_cache(maxsize=4)
def _load_query(grammar: str):
    with open(os.path.join(_QUERY_DIR, 'typescript.scm'), 'r', encoding='utf-8') as fh:
        src = fh.read()
    if grammar == 'tsx':
        with open(os.path.join(_QUERY_DIR, 'tsx.scm'), 'r', encoding='utf-8') as fh:
            src += '\n' + fh.read()
    return get_language(grammar).query(src)


# ───────────────────────────────  helpers  ──────────────────────────────────

def _is_pascal_case(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _key(node) -> Tuple[int, int]:
    """Stable identity for a tree-sitter node. The Python bindings hand back a
    fresh Node wrapper on every ``.parent`` / ``.children`` access, so the
    builtin ``id()`` is useless for "is this the same node" checks -- the byte
    span is."""
    return (node.start_byte, node.end_byte)


def _node_text(node, data: bytes) -> str:
    return data[node.start_byte:node.end_byte].decode('utf-8', 'replace')


def _name_of(node, data: bytes) -> Optional[str]:
    field = node.child_by_field_name('name')
    if field is not None:
        return _node_text(field, data)
    return None


def _outer_statement(node):
    """Climb from a captured def node to the statement that should own the
    node's ``code`` / line range (so ``export const`` / ``export default`` and
    the trailing ``;`` are included)."""
    n = node
    if n.type == 'variable_declarator':
        p = n.parent
        while p is not None and p.type not in ('lexical_declaration', 'variable_declaration'):
            p = p.parent
        n = p or n
    if n.parent is not None and n.parent.type == 'export_statement':
        n = n.parent
    return n


def _leading_comment_start(outer, data: bytes) -> int:
    """Byte offset of a contiguous run of comment siblings immediately above
    *outer* (JSDoc / line comments), or ``outer.start_byte``."""
    start = outer.start_byte
    sib = outer.prev_sibling
    while sib is not None and sib.type == 'comment':
        # only attach if directly adjacent (no blank line between)
        between = data[sib.end_byte:start]
        if between.count(b'\n') > 1:
            break
        start = sib.start_byte
        sib = sib.prev_sibling
    return start


def _first_body(node):
    """First ``statement_block`` / ``class_body`` in *node*'s subtree (BFS,
    shallow)."""
    stack = list(node.children)
    while stack:
        cur = stack.pop(0)
        if cur.type in ('statement_block', 'class_body'):
            return cur
        stack.extend(cur.children)
    return None


_SKELETON_COMMENT_MAX = 280


def _skeleton(inner, outer, data: bytes) -> str:
    lead = _leading_comment_start(outer, data)
    comment = data[lead:outer.start_byte].decode('utf-8', 'replace').strip()
    if len(comment) > _SKELETON_COMMENT_MAX:
        comment = comment[:_SKELETON_COMMENT_MAX].rstrip() + ' ... */'

    body = _first_body(inner)
    if body is not None:
        sig = data[outer.start_byte:body.start_byte].decode('utf-8', 'replace').rstrip()
        core = f'{sig} {{ /* ... */ }}'
    else:
        core = data[outer.start_byte:outer.end_byte].decode('utf-8', 'replace').rstrip()

    skel = f'{comment}\n{core}'.strip() if comment else core.strip()
    if len(skel) > _SKELETON_MAX:
        skel = skel[:_SKELETON_MAX].rstrip() + ' /* ... */'
    return skel


_NESTED_FN_TYPES = ('function_declaration', 'function', 'arrow_function',
                    'method_definition', 'generator_function_declaration')


# method names whose callback arg is data-transform, not a component/def wrapper
_ITER_METHODS = frozenset({
    'map', 'filter', 'reduce', 'reduceRight', 'forEach', 'flatMap', 'some',
    'every', 'find', 'findIndex', 'findLast', 'sort', 'partition', 'groupBy',
})


def _find_inner_function(call_node):
    """The function passed as an argument to a HOC-style call -- ``forwardRef(fn)``,
    ``memo(fn)``, ``memo(forwardRef(fn))``, ``styled.div(...)``. Returns the
    arrow/function node, or None -- also None for data-transform calls like
    ``items.map(fn)`` so those value bindings are not mistaken for defs."""
    callee = call_node.child_by_field_name('function')
    if callee is not None and callee.type == 'member_expression':
        prop = callee.child_by_field_name('property')
        if prop is not None and prop.text.decode('utf-8', 'replace') in _ITER_METHODS:
            return None
    args = call_node.child_by_field_name('arguments')
    if args is None:
        return None
    for ch in args.children:
        if ch.type in ('arrow_function', 'function'):
            return ch
        if ch.type == 'call_expression':
            deep = _find_inner_function(ch)
            if deep is not None:
                return deep
    return None


def _effective_body(node):
    """The node whose subtree is the function/class *body* -- unwraps
    ``const C = () => (...)`` / ``field = () => {}`` to the arrow/function, and
    ``const C = forwardRef(fn)`` to ``fn``."""
    if node.type in ('variable_declarator', 'public_field_definition'):
        node = node.child_by_field_name('value') or node
    if node.type == 'call_expression':
        return _find_inner_function(node) or node
    return node


def _subtree_has_jsx(inner) -> bool:
    """True if *inner*'s body contains a JSX element, without descending into
    nested function definitions (so an inner render-prop component is not
    attributed to its parent)."""
    body = _effective_body(inner)
    stack = list(body.children)
    while stack:
        cur = stack.pop()
        if cur.type in _JSX_ELEMENT_TYPES:
            return True
        if cur.type in _NESTED_FN_TYPES:
            continue
        stack.extend(cur.children)
    return False


# JSX attribute values worth recording on a `renders` edge: expression bindings
# (event handlers, data, refs). String / numeric / style literals carry no wiring.
_JSX_WIRING_VALUE_TYPES = frozenset({
    'identifier', 'member_expression', 'call_expression',
    'arrow_function', 'function',
})
_JSX_PROP_MAX = 80        # truncate one bound expression's source text
_JSX_PROPS_PER_SITE = 12  # cap props recorded per JSX tag


def _jsx_tag_name(el, data: bytes) -> Optional[str]:
    """Tag name of a jsx_opening_element / jsx_self_closing_element. For
    ``<Foo.Bar />`` returns ``Bar`` (matched name-only, like the call heuristic)."""
    nm = el.child_by_field_name('name')
    if nm is None:
        return None
    if nm.type == 'member_expression':
        prop = nm.child_by_field_name('property')
        return _node_text(prop, data) if prop is not None else None
    if nm.type in ('identifier', 'property_identifier'):
        return _node_text(nm, data)
    return None


def _collapse_expr(node, data: bytes) -> str:
    """One-line summary of a bound JSX expression. An inline arrow/function is
    reduced to its parameter list + ``=> …`` (the body is noise for a wiring
    map); a bare reference / member / call is passed through, whitespace-collapsed
    and length-capped."""
    if node.type == 'arrow_function':
        arrow = next((c for c in node.children if c.type == '=>'), None)
        if arrow is not None:
            head = data[node.start_byte:arrow.end_byte].decode('utf-8', 'replace')
            return ' '.join(head.split()) + ' …'
    if node.type == 'function':
        body = _first_body(node)
        if body is not None:
            head = data[node.start_byte:body.start_byte].decode('utf-8', 'replace')
            return ' '.join(head.split()) + ' {…}'
    txt = ' '.join(_node_text(node, data).split())
    return txt[:_JSX_PROP_MAX] + ('…' if len(txt) > _JSX_PROP_MAX else '')


def _jsx_wiring_props(el, data: bytes) -> Dict[str, str]:
    """prop name -> collapsed source of its bound expression, for
    expression-valued attributes only (``onSave={handleSave}``, ``data={rows}``);
    string / numeric / element literals and ``{...spread}`` are skipped."""
    out: Dict[str, str] = {}
    for ch in el.children:
        if ch.type != 'jsx_attribute':
            continue
        kids = ch.children
        if not kids or kids[0].type != 'property_identifier':
            continue
        if len(kids) < 3 or kids[1].type != '=':
            continue                       # boolean shorthand -> no wiring
        val = kids[2]
        if val.type != 'jsx_expression':
            continue                       # string / element literal
        inner = next((c for c in val.children
                      if c.type not in ('{', '}', 'comment')), None)
        if inner is None or inner.type not in _JSX_WIRING_VALUE_TYPES:
            continue
        out[_node_text(kids[0], data)] = _collapse_expr(inner, data)
        if len(out) >= _JSX_PROPS_PER_SITE:
            break
    return out


# ────────────────────────────  per-file analysis  ───────────────────────────

def analyze_ts_file(abs_path: str, grammar: str) -> Tuple[List[dict], List[dict]]:
    """Parse one file. Returns ``(entities, imports)``.

    ``entities`` -- list of dicts: ``name`` (dotted), ``type``, ``code``,
    ``start_line``, ``end_line``, ``parent_type``, ``skeleton``, ``is_component``,
    ``calls`` (list[(name, line)]), ``renders`` (list[{name, line, props}]),
    ``heritage`` (list[str]).

    ``imports`` -- list of dicts: ``source`` (raw specifier), ``names``
    (list[str]; ``'*'`` for namespace/side-effect/re-export-all), ``kind``
    (``'import'`` | ``'reexport'``).
    """
    with open(abs_path, 'rb') as fh:
        data = fh.read()

    tree = get_parser(grammar).parse(data)
    root = tree.root_node
    captures = _load_query(grammar).captures(root)

    def_nodes: List[Tuple[object, str]] = []
    call_caps: List[object] = []
    jsx_caps: List[object] = []
    heritage_caps: List[object] = []
    import_nodes: List[object] = []

    for node, cap in captures:
        if cap in _DEF_KIND:
            def_nodes.append((node, _DEF_KIND[cap]))
        elif cap == 'call.name':
            call_caps.append(node)
        elif cap == 'jsx.element':
            jsx_caps.append(node)
        elif cap == 'heritage.name':
            heritage_caps.append(node)
        elif cap == 'import.node':
            import_nodes.append(node)

    def_kinds = {_key(n): kind for n, kind in def_nodes}

    def enclosing_def(node):
        """Nearest ancestor that is a captured def node (or None)."""
        p = node.parent
        while p is not None:
            if _key(p) in def_kinds:
                return p
            p = p.parent
        return None

    # ---- materialise entities -------------------------------------------------
    entities: List[dict] = []
    entity_by_id: Dict[Tuple[int, int], dict] = {}

    for node, kind in def_nodes:
        # enclosing chain of captured defs, nearest first. Named nested defs are
        # materialised too (dotted name, like upstream's Python visitor) -- inside
        # a 6k-line component the real sub-components / hooks live nested. A depth
        # cap keeps deeply-nested callback helpers out.
        chain = []
        anc = enclosing_def(node)
        while anc is not None:
            chain.append(anc)
            anc = enclosing_def(anc)
        if len(chain) > _MAX_NEST_DEPTH:
            continue

        name = _name_of(node, data)
        if not name:
            continue

        # `const X = someCall(...)` matched def.wrapped: keep it only when the
        # call actually wraps a function (forwardRef/memo/...), else it is just a
        # value binding (`const x = compute()`) and not an entity.
        if node.type == 'variable_declarator':
            value = node.child_by_field_name('value')
            if value is not None and value.type == 'call_expression' \
                    and _find_inner_function(value) is None:
                continue

        dotted = '.'.join([_name_of(c, data) or '?' for c in reversed(chain)] + [name])
        parent_type = def_kinds[_key(chain[0])] if chain else None

        outer = _outer_statement(node)
        is_component = (
            grammar == 'tsx'
            and kind == NODE_TYPE_FUNCTION
            and _is_pascal_case(name)
            and _subtree_has_jsx(node)
        )
        ent = {
            'name': dotted,
            'type': kind,
            'code': _node_text(outer, data),
            'start_line': outer.start_point[0] + 1,
            'end_line': outer.end_point[0] + 1,
            'parent_type': parent_type,
            'skeleton': _skeleton(node, outer, data),
            'is_component': is_component,
            'calls': [],
            'renders': [],
            'heritage': [],
        }
        entities.append(ent)
        entity_by_id[_key(node)] = ent

    # ---- attribute calls / jsx / heritage to their entity -------------------
    def owner_entity(node) -> Optional[dict]:
        anc = enclosing_def(node)
        while anc is not None:
            if _key(anc) in entity_by_id:
                return entity_by_id[_key(anc)]
            anc = enclosing_def(anc)
        return None

    for cnode in call_caps:
        nm = _node_text(cnode, data)
        if not nm or nm in _CALL_STOPLIST:
            continue
        ent = owner_entity(cnode)
        if ent is not None and nm != ent['name'].split('.')[-1]:
            ent['calls'].append((nm, cnode.start_point[0] + 1))

    for el in jsx_caps:
        comp = _jsx_tag_name(el, data)
        if not comp or not _is_pascal_case(comp):
            continue
        ent = owner_entity(el)
        if ent is not None:
            ent['renders'].append({
                'name': comp,
                'line': el.start_point[0] + 1,
                'props': _jsx_wiring_props(el, data),
            })

    for hnode in heritage_caps:
        # heritage.name sits inside class_heritage -> class_declaration
        p = hnode.parent
        while p is not None and p.type not in _CLASS_NODE_TYPES:
            p = p.parent
        if p is not None and _key(p) in entity_by_id:
            entity_by_id[_key(p)]['heritage'].append(_node_text(hnode, data))

    # ---- imports ----------------------------------------------------------
    imports: List[dict] = []
    for inode in import_nodes:
        src_field = inode.child_by_field_name('source')
        if src_field is None:
            continue
        source = _node_text(src_field, data).strip('\'"`')
        if not source:
            continue
        names = _extract_import_names(inode, data)
        kind = 'reexport' if inode.type == 'export_statement' else 'import'
        imports.append({'source': source, 'names': names, 'kind': kind})

    return entities, imports


def _extract_import_names(inode, data: bytes) -> List[str]:
    """Named bindings introduced by an import / re-export. ``'*'`` stands for a
    namespace import, a bare side-effect import, or ``export * from``."""
    names: List[str] = []
    clause = None
    for ch in inode.children:
        if ch.type in ('import_clause', 'export_clause', 'import_require_clause'):
            clause = ch
            break
    if clause is None:
        # `import "./x"` (side effect) or `export * from "./x"`
        return ['*']

    for ch in clause.children if clause.type != 'export_clause' else [clause]:
        t = ch.type
        if t == 'identifier':  # default import  /  import x = require(...)
            names.append(_node_text(ch, data))
        elif t == 'namespace_import':
            names.append('*')
        elif t in ('named_imports', 'export_clause'):
            for spec in ch.children:
                if spec.type in ('import_specifier', 'export_specifier'):
                    nm = spec.child_by_field_name('name')
                    if nm is not None:
                        names.append(_node_text(nm, data))
    return names or ['*']


# ────────────────────────────  graph assembly  ─────────────────────────────

def _skip_dir(rel_dir: str) -> bool:
    if is_skip_dir(rel_dir):
        return True
    return any(part in SKIP_DIRS for part in rel_dir.replace('\\', '/').split('/'))


def build_ts_graph(repo_path: str, fuzzy_search: bool = True, verbose: bool = False) -> nx.MultiDiGraph:
    repo_path = os.path.abspath(repo_path)
    graph = nx.MultiDiGraph()
    graph.add_node('/', type=NODE_TYPE_DIRECTORY)

    file_imports: Dict[str, List[dict]] = {}
    files_seen = 0
    parse_errors = 0

    for root, dirs, files in os.walk(repo_path):
        rel_dir = os.path.relpath(root, repo_path).replace(os.sep, '/')
        if rel_dir == '.':
            rel_dir = '/'
        elif _skip_dir(rel_dir):
            dirs[:] = []
            continue
        # prune child dirs in-place so os.walk does not descend
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.git')]

        src_files = [f for f in files if f.endswith(SOURCE_EXTS)
                     and not f.endswith('.d.ts')]
        if not src_files:
            continue

        # register this dir + all ancestors (only dirs that end up holding code)
        _ensure_dir_chain(graph, rel_dir)

        for fname in src_files:
            abs_path = os.path.join(root, fname)
            if os.path.islink(abs_path):
                continue
            rel_file = os.path.relpath(abs_path, repo_path).replace(os.sep, '/')
            grammar = GRAMMAR_BY_EXT[os.path.splitext(fname)[1]]
            try:
                with open(abs_path, 'r', encoding='utf-8') as fh:
                    content = fh.read()
            except (UnicodeDecodeError, OSError):
                continue

            try:
                entities, imports = analyze_ts_file(abs_path, grammar)
            except Exception as exc:  # tree-sitter is lenient; guard anyway
                parse_errors += 1
                if verbose:
                    print(f'  ! parse failed {rel_file}: {exc}', file=sys.stderr)
                entities, imports = [], []

            files_seen += 1
            graph.add_node(rel_file, type=NODE_TYPE_FILE, code=content, language=grammar)
            graph.add_edge(rel_dir if rel_dir != '/' else '/', rel_file, type=EDGE_TYPE_CONTAINS)
            file_imports[rel_file] = imports

            for ent in entities:
                nid = f'{rel_file}:{ent["name"]}'
                graph.add_node(
                    nid, type=ent['type'], code=ent['code'],
                    start_line=ent['start_line'], end_line=ent['end_line'],
                    parent_type=ent['parent_type'], skeleton=ent['skeleton'],
                    is_component=ent['is_component'],
                    _calls=ent['calls'], _renders=ent['renders'], _heritage=ent['heritage'],
                )
            for ent in entities:
                nid = f'{rel_file}:{ent["name"]}'
                parts = ent['name'].split('.')
                if len(parts) == 1:
                    graph.add_edge(rel_file, nid, type=EDGE_TYPE_CONTAINS)
                else:
                    parent_nid = f'{rel_file}:{".".join(parts[:-1])}'
                    if graph.has_node(parent_nid):
                        graph.add_edge(parent_nid, nid, type=EDGE_TYPE_CONTAINS)
                    else:
                        graph.add_edge(rel_file, nid, type=EDGE_TYPE_CONTAINS)

    if verbose:
        print(f'  parsed {files_seen} files ({parse_errors} parse errors)')

    _add_import_edges(graph, repo_path, file_imports, verbose=verbose)
    _add_reference_edges(graph, file_imports, fuzzy_search=fuzzy_search, verbose=verbose)

    # strip working attributes
    for _, attrs in graph.nodes(data=True):
        for k in ('_calls', '_renders', '_heritage'):
            attrs.pop(k, None)

    return graph


def _ensure_dir_chain(graph: nx.MultiDiGraph, rel_dir: str) -> None:
    if rel_dir == '/' or graph.has_node(rel_dir):
        return
    parts = rel_dir.split('/')
    for i in range(len(parts)):
        d = '/'.join(parts[: i + 1])
        parent = '/'.join(parts[:i]) or '/'
        if not graph.has_node(d):
            graph.add_node(d, type=NODE_TYPE_DIRECTORY)
            graph.add_edge(parent, d, type=EDGE_TYPE_CONTAINS)


def _add_import_edges(graph, repo_path, file_imports, verbose=False):
    alias_map = load_alias_map(repo_path)
    if verbose:
        print(f'  alias map: {alias_map or "{}"}')
    edges = 0
    for rel_file, imports in file_imports.items():
        for imp in imports:
            target = resolve_import(imp['source'], rel_file, repo_path, alias_map)
            if not target or not graph.has_node(target):
                continue
            hooked = False
            for nm in imp['names']:
                if nm and nm != '*':
                    ent = f'{target}:{nm}'
                    if graph.has_node(ent):
                        graph.add_edge(rel_file, ent, type=EDGE_TYPE_IMPORTS, alias=None)
                        edges += 1
                        hooked = True
            if not hooked:
                graph.add_edge(rel_file, target, type=EDGE_TYPE_IMPORTS, alias=None)
                edges += 1
    if verbose:
        print(f'  imports edges: {edges}')


def _add_reference_edges(graph, file_imports, fuzzy_search=True, verbose=False):
    """`invokes`, `renders`, `inherits` -- all name-matched, disambiguated by
    import binding then file scope."""
    # global name index: last-segment name -> [node_id]
    by_name: Dict[str, List[str]] = defaultdict(list)
    file_of: Dict[str, str] = {}
    for nid, attrs in graph.nodes(data=True):
        if attrs.get('type') in (NODE_TYPE_CLASS, NODE_TYPE_FUNCTION):
            by_name[nid.split(':')[-1].split('.')[-1]].append(nid)
            file_of[nid] = nid.split(':')[0]

    # per-file import sets, at two granularities: the exact entity a file pulls
    # in by name (`import { FrameIcon } from './icons'` -> icons.tsx:FrameIcon),
    # and the coarser set of files it imports from at all.
    imported_files: Dict[str, set] = defaultdict(set)
    imported_entities: Dict[str, set] = defaultdict(set)
    for src, dst, attrs in graph.edges(data=True):
        if attrs.get('type') == EDGE_TYPE_IMPORTS:
            imported_files[src].add(dst.split(':')[0])
            if ':' in dst:
                imported_entities[src].add(dst)

    def candidates(caller_nid: str, callee_name: str) -> List[str]:
        cands = by_name.get(callee_name, [])
        if not cands:
            return []
        caller_file = file_of.get(caller_nid, '')
        # 1. the caller's file imports this exact entity by name, or it is
        #    defined in the caller's own file -- unambiguous, use only these.
        bound = [c for c in cands
                 if c in imported_entities.get(caller_file, ())
                 or file_of.get(c) == caller_file]
        if bound:
            return bound
        # 2. the candidate lives in some file the caller imports from.
        allowed = imported_files.get(caller_file, set()) | {caller_file}
        scoped = [c for c in cands if file_of.get(c) in allowed]
        if scoped:
            return scoped
        # 3. nothing in scope -- global name match (heuristic, opt-in).
        return cands if fuzzy_search else []

    n_inv = n_ren = n_inh = 0
    for nid, attrs in list(graph.nodes(data=True)):
        ntype = attrs.get('type')
        if ntype not in (NODE_TYPE_CLASS, NODE_TYPE_FUNCTION):
            continue

        calls_by_name: Dict[str, List[int]] = defaultdict(list)
        for nm, ln in attrs.get('_calls', []):
            calls_by_name[nm].append(ln)
        for callee_name, lines in calls_by_name.items():
            for tgt in candidates(nid, callee_name):
                if tgt != nid:
                    graph.add_edge(nid, tgt, type=EDGE_TYPE_INVOKES,
                                   call_lines=sorted(set(lines))[:20])
                    n_inv += 1

        renders_by_name: Dict[str, List[dict]] = defaultdict(list)
        for r in attrs.get('_renders', []):
            renders_by_name[r['name']].append(r)
        for comp_name, sites in renders_by_name.items():
            for tgt in candidates(nid, comp_name):
                if tgt != nid and graph.nodes[tgt].get('is_component'):
                    ordered = sorted(sites, key=lambda s: s['line'])
                    graph.add_edge(
                        nid, tgt, type=EDGE_TYPE_RENDERS,
                        jsx_lines=sorted({s['line'] for s in sites})[:20],
                        sites=[{'line': s['line'], 'props': s['props']}
                               for s in ordered if s['props']][:8] or None,
                    )
                    n_ren += 1

        for base_name in set(attrs.get('_heritage', [])):
            for tgt in candidates(nid, base_name):
                if tgt != nid and graph.nodes[tgt].get('type') == NODE_TYPE_CLASS:
                    graph.add_edge(nid, tgt, type=EDGE_TYPE_INHERITS)
                    n_inh += 1

    if verbose:
        print(f'  invokes edges: {n_inv}   renders edges: {n_ren}   inherits edges: {n_inh}')


# ─────────────────────────────────  CLI  ──────────────────────────────────

def _stats(graph: nx.MultiDiGraph) -> str:
    n_by_t: Dict[str, int] = defaultdict(int)
    for _, a in graph.nodes(data=True):
        n_by_t[a.get('type', '?')] += 1
    e_by_t: Dict[str, int] = defaultdict(int)
    for _, _, a in graph.edges(data=True):
        e_by_t[a.get('type', '?')] += 1
    return (f'nodes: {dict(n_by_t)}  ({graph.number_of_nodes()} total)\n'
            f'edges: {dict(e_by_t)}  ({graph.number_of_edges()} total)')


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', required=True, help='path to the repository root')
    ap.add_argument('--output', help='write the graph as a pickle to this path')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args(argv)

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        ap.error(f'not a directory: {repo}')

    t0 = time.time()
    graph = build_ts_graph(repo, verbose=not args.quiet)
    dt = time.time() - t0

    print(_stats(graph))
    print(f'built in {dt:.2f}s')

    if args.output:
        with open(args.output, 'wb') as fh:
            pickle.dump(graph, fh)
        print(f'wrote {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
