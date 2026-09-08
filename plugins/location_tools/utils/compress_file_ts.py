"""tree-sitter skeletonizer for TypeScript / TSX / JS / JSX.

TS counterpart of ``compress_file.py`` (which is libcst, Python-only). Given the
raw source of a file or a class/function entity, returns the same text with every
function / method / arrow body replaced by ``{ ... }`` -- signatures, JSDoc,
imports, type declarations and top-level bindings are kept so the agent sees the
shape of a large file without its full contents.

Same entry point as the Python version::

    get_skeleton(raw_code, keep_constant=True, language='tsx') -> str

``language`` accepts ``'typescript'`` / ``'tsx'`` (aliases: ``ts``, ``tsx``,
``js``, ``jsx``) or, via ``filename``, is inferred from the extension. On any
parse failure the input is returned unchanged.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

from tree_sitter_languages import get_parser

_GRAMMAR_BY_EXT = {
    '.ts': 'typescript', '.mts': 'typescript', '.cts': 'typescript',
    '.tsx': 'tsx', '.jsx': 'tsx', '.js': 'tsx', '.mjs': 'tsx', '.cjs': 'tsx',
}
_LANG_ALIASES = {
    'typescript': 'typescript', 'ts': 'typescript',
    'tsx': 'tsx', 'jsx': 'tsx', 'js': 'tsx', 'javascript': 'tsx',
}

# function-ish nodes whose statement_block body gets elided
_FN_TYPES = frozenset({
    'function_declaration', 'generator_function_declaration', 'method_definition',
    'arrow_function', 'function', 'function_expression',
})
_BODY_PLACEHOLDER = '{ ... }'


def _resolve_grammar(language: Optional[str], filename: Optional[str]) -> str:
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext in _GRAMMAR_BY_EXT:
            return _GRAMMAR_BY_EXT[ext]
    if language:
        return _LANG_ALIASES.get(language.lower(), 'tsx')
    return 'tsx'


def _collect_bodies(root) -> List[Tuple[int, int]]:
    """Byte ranges of the shallowest function/method bodies (``statement_block``).
    Nested functions live inside these ranges and are elided with the parent."""
    ranges: List[Tuple[int, int]] = []

    def walk(node):
        for ch in node.children:
            if ch.type in _FN_TYPES:
                body = ch.child_by_field_name('body')
                if body is not None and body.type == 'statement_block':
                    ranges.append((body.start_byte, body.end_byte))
                    continue  # do not descend -- nested defs go with it
                walk(ch)  # arrow with expression body: nothing to elide here
            else:
                walk(ch)

    walk(root)
    return ranges


def get_skeleton(raw_code: str, keep_constant: bool = True,
                 language: Optional[str] = None, filename: Optional[str] = None) -> str:
    """Return *raw_code* with function bodies collapsed to ``{ ... }``.

    ``keep_constant`` is accepted for signature parity with the Python
    skeletonizer; TS top-level bindings are always kept (they carry types and
    config that matter for localization).
    """
    grammar = _resolve_grammar(language, filename)
    data = raw_code.encode('utf-8')
    try:
        tree = get_parser(grammar).parse(data)
    except Exception:
        return raw_code
    if tree.root_node.has_error and tree.root_node.child_count == 0:
        return raw_code

    ranges = _collect_bodies(tree.root_node)
    if not ranges:
        return raw_code

    ranges.sort()
    out: List[str] = []
    prev = 0
    for start, end in ranges:
        if start < prev:  # overlapping (defensive) -- skip
            continue
        out.append(data[prev:start].decode('utf-8', 'replace'))
        out.append(_BODY_PLACEHOLDER)
        prev = end
    out.append(data[prev:].decode('utf-8', 'replace'))
    return ''.join(out)


if __name__ == '__main__':  # tiny smoke test
    sample = '''
import { useState } from "react";

/** A counter. */
export const Counter = ({ start = 0 }: { start?: number }) => {
  const [n, setN] = useState(start);
  function bump() { setN((v) => v + 1); }
  return <button onClick={bump}>{n}</button>;
};

export class Store {
  private items: string[] = [];
  add(x: string) { this.items.push(x); }
}
'''
    print(get_skeleton(sample, language='tsx'))
