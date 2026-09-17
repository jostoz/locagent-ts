"""Incremental-refresh equivalence check for the TS/TSX graph.

``patch_ts_graph`` re-derives only the part of the graph a changed file can
affect. That closure is easy to get subtly wrong -- dropping a *directory* node
along with the changed file, for instance, silently deleted every sibling file's
edges in the same directory, and a synthetic fixture whose directory held a single
file could not see it. This module pins the behaviour: after each kind of edit, a
patched graph must equal a full rebuild, node for node and annotated edge for
annotated edge, and the fixture keeps a second file in the changed file's
directory so the sibling case stays covered.

    python -m dependency_graph.ts_patch_check          # runs the 6 edit cases
    python -m dependency_graph.ts_patch_check --verbose

Exit status is 0 when every case is equivalent, 1 otherwise.
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from dependency_graph.ts_build_graph import build_ts_graph, patch_ts_graph

TS_CONFIG = json.dumps({'compilerOptions': {'baseUrl': '.', 'paths': {'@/*': ['./src/*']}}})

_FILES: Dict[str, str] = {
    'tsconfig.json': TS_CONFIG,
    'src/lib/utils.ts': """import { createContext } from 'react';
export const UI_CONTEXT = createContext<{ theme: string } | null>(null);
/** join class names */
export function cn(...parts: Array<string | false>): string {
  return parts.filter(Boolean).join(' ');
}
""",
    # sibling of the file the first case edits, in the same directory, with its
    # own imports and calls: the fixture that catches a too-wide recompute set
    'src/lib/format.ts': """import { cn } from './utils';
export function formatDate(d: Date): string {
  return cn(String(d.getTime()));
}
""",
    'src/components/Button.tsx': """import { cn } from '@/lib/utils';
export const Button = ({ label, onClick }: { label: string; onClick?: () => void }) => (
  <button className={cn('btn')} onClick={onClick}>{label}</button>
);
""",
    'src/components/Panel.tsx': """import { Button } from './Button';
export function Panel({ onSave }: { onSave?: () => void }) {
  return (
    <div className="panel">
      <Button label="save" onClick={onSave} />
    </div>
  );
}
""",
    'src/board/Board.tsx': """import { Panel } from '@/components/Panel';
import { UI_CONTEXT } from '@/lib/utils';
import { cn } from '@/lib/utils';
export function Board() {
  return (
    <UI_CONTEXT.Provider value={{ theme: 'dark' }}>
      <div className={cn('board')}>
        <Panel />
      </div>
    </UI_CONTEXT.Provider>
  );
}
""",
}

_EDITS: Dict[str, Dict[str, Optional[str]]] = {
    'add-export-and-call': {
        'src/lib/utils.ts': _FILES['src/lib/utils.ts'] + """
export function formatLabel(text: string): string {
  return text.trim();
}
""",
        'src/board/Board.tsx': _FILES['src/board/Board.tsx']
        .replace('export function Board() {',
                 "import { formatLabel } from '@/lib/utils';\n"
                 'export function Board() {\n  const label = formatLabel("board");')
        .replace('<Panel />', '<Panel data-label={label} />'),
    },
    'prop-change': {
        'src/components/Panel.tsx': _FILES['src/components/Panel.tsx'].replace(
            '<Button label="save" onClick={onSave} />',
            '<Button label="archive" onMouseDown={onSave} />'),
    },
    'delete-file': {'src/components/Panel.tsx': None},
    'new-file': {
        'src/components/Toolbar.tsx': """import { Button } from './Button';
export function Toolbar({ onClose }: { onClose: () => void }) {
  return <Button label="close" onClick={onClose} />;
}
""",
        'src/components/Panel.tsx': _FILES['src/components/Panel.tsx']
        .replace('    </div>', '      <Toolbar onClose={onSave ?? (() => {})} />\n    </div>')
        .replace("import { Button } from './Button';",
                 "import { Button } from './Button';\nimport { Toolbar } from './Toolbar';"),
    },
    'context-rename': {
        'src/lib/utils.ts': _FILES['src/lib/utils.ts'].replace('UI_CONTEXT', 'THEME_CONTEXT'),
        'src/board/Board.tsx': _FILES['src/board/Board.tsx'].replace('UI_CONTEXT', 'THEME_CONTEXT'),
    },
    # a batch: two unrelated files plus a deletion in one refresh
    'multi-file-batch': {
        'src/lib/format.ts': _FILES['src/lib/format.ts'].replace(
            'export function formatDate', 'export function formatDay'),
        'src/components/Button.tsx': _FILES['src/components/Button.tsx'].replace(
            "className={cn('btn')}", "className={cn('btn', 'primary')}"),
        'src/components/Panel.tsx': None,
    },
}


def _write_repo(root: str, files: Dict[str, str]) -> None:
    if os.path.isdir(root):
        shutil.rmtree(root)
    for rel, text in files.items():
        path = os.path.join(root, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)


def _fingerprint(graph) -> Tuple[dict, list]:
    nodes = {n: (d.get('type'), d.get('is_component'), d.get('is_context'),
                 d.get('is_exported'), d.get('start_line'), d.get('end_line'),
                 d.get('parent_type'))
             for n, d in graph.nodes(data=True)}
    edges = sorted(
        (u, d.get('type'), v,
         json.dumps(d.get('call_lines'), sort_keys=True),
         json.dumps(d.get('jsx_lines'), sort_keys=True),
         json.dumps(d.get('sites'), sort_keys=True))
        for u, v, d in graph.edges(data=True))
    return nodes, edges


def _diff(patch_fp, full_fp, limit: int = 8) -> List[str]:
    (na, ea), (nb, eb) = patch_fp, full_fp
    problems: List[str] = []
    if na != nb:
        for n in sorted(set(na) ^ set(nb)):
            problems.append(f'node only in one graph: {n}')
        for n in sorted(set(na) & set(nb)):
            if na[n] != nb[n]:
                problems.append(f'node attrs differ: {n}  patched={na[n]} rebuilt={nb[n]}')
    if ea != eb:
        problems += [f'edge only in patched: {e}' for e in ea if e not in eb][:limit]
        problems += [f'edge only in rebuilt: {e}' for e in eb if e not in ea][:limit]
        problems.append(f'edge count patched={len(ea)} rebuilt={len(eb)}')
    return problems


def run(verbose: bool = False, root: Optional[str] = None) -> int:
    root = root or os.path.join(tempfile.gettempdir(), 'locagent-ts-patch-check')
    _write_repo(root, _FILES)
    base = build_ts_graph(root)
    if verbose:
        print(f'baseline: {base.number_of_nodes()} nodes, {base.number_of_edges()} edges')

    failures = 0
    for name, edits in _EDITS.items():
        files = dict(_FILES)
        for rel, text in edits.items():
            if text is None:
                files.pop(rel, None)
            else:
                files[rel] = text
        _write_repo(root, files)

        graph = copy.deepcopy(base)          # deepcopy: graph.copy() aliases attrs
        t0 = time.perf_counter()
        stats = patch_ts_graph(graph, root, list(edits))
        patch_ms = (time.perf_counter() - t0) * 1000
        rebuilt = build_ts_graph(root)

        problems = _diff(_fingerprint(graph), _fingerprint(rebuilt))
        status = 'OK' if not problems else 'MISMATCH'
        failures += bool(problems)
        print(f'[{status}] {name}: patch {patch_ms:.1f}ms '
              f'({stats["changed"]} changed, {stats["recomputed"]} recomputed, '
              f'{stats["parsed"]} parsed, {stats["removed"]} nodes removed) '
              f'| {graph.number_of_nodes()}n/{graph.number_of_edges()}e vs '
              f'{rebuilt.number_of_nodes()}n/{rebuilt.number_of_edges()}e')
        for problem in problems:
            print(f'      {problem}')

    print(f'patch_check: {len(_EDITS) - failures}/{len(_EDITS)} cases equivalent')
    return 1 if failures else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    return run(verbose='--verbose' in args or '-v' in args)


if __name__ == '__main__':
    raise SystemExit(main())
