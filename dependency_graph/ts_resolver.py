"""TypeScript / TSX module + path-alias import resolution for the LocAgent-TS
graph builder.

Replaces build_graph.py's Python-specific ``find_imports`` / ``resolve_module``.
The parsing of ``import`` / ``export ... from`` statement *structure* happens in
``ts_build_graph.py`` (tree-sitter); this module turns a raw module specifier
string (``"./foo"``, ``"@/lib/utils"``, ``"react"``) into a repo-relative POSIX
file path, or ``None`` when it does not resolve to a file inside the repo
(bare specifiers / externals -> no edge in v1, per the port plan).
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from typing import Dict, List, Optional

# Extension probe order when a specifier has no extension.
_EXT_CANDIDATES = ('.ts', '.tsx', '.d.ts', '.js', '.jsx', '.mts', '.cts', '.mjs', '.cjs')
_INDEX_BASENAMES = tuple(f'index{ext}' for ext in _EXT_CANDIDATES)

# tsconfig files to consult at the repo root, most specific first.
_TSCONFIG_NAMES = ('tsconfig.json', 'tsconfig.app.json', 'tsconfig.base.json')


def _strip_jsonc(text: str) -> str:
    """Best-effort strip of // and /* */ comments and trailing commas so a
    tsconfig (JSONC) can go through ``json.loads``."""
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'(^|[^:])//[^\n\r]*', r'\1', text)
    text = re.sub(r',(\s*[}\]])', r'\1', text)
    return text


def load_alias_map(repo_path: str) -> Dict[str, List[str]]:
    """Build ``{ alias_prefix: [target_prefix, ...] }`` from tsconfig ``paths``.

    Prefixes keep the trailing ``/`` and drop a ``*`` glob so lookup is a plain
    ``startswith``.  ``baseUrl`` is folded into the targets.  Falls back to
    ``{'@/': ['src/']}`` when a ``src`` dir exists and no config parses.
    """
    for name in _TSCONFIG_NAMES:
        cfg_path = os.path.join(repo_path, name)
        if not os.path.isfile(cfg_path):
            continue
        try:
            with open(cfg_path, 'r', encoding='utf-8') as fh:
                cfg = json.loads(_strip_jsonc(fh.read()))
        except (ValueError, OSError):
            continue
        opts = cfg.get('compilerOptions', {}) or {}
        base_url = (opts.get('baseUrl', '.') or '.').replace('\\', '/')
        paths = opts.get('paths', {}) or {}
        alias_map: Dict[str, List[str]] = {}
        for pattern, targets in paths.items():
            # "@/*" -> key "@/", "@app" -> key "@app"
            key = pattern[:-1] if pattern.endswith('*') else pattern
            resolved: List[str] = []
            for target in targets:
                # "./src/*" -> "./src", "src/lib" -> "src/lib"
                t = target[:-1] if target.endswith('*') else target
                joined = posixpath.normpath(posixpath.join(base_url, t))
                if joined in ('.', './'):
                    joined = ''
                elif joined.startswith('./'):
                    joined = joined[2:]
                resolved.append(joined)  # no trailing slash; joined via posixpath later
            alias_map[key] = resolved
        if alias_map:
            return alias_map

    if os.path.isdir(os.path.join(repo_path, 'src')):
        return {'@/': ['src/']}
    return {}


def _first_existing(repo_path: str, rel_no_ext: str) -> Optional[str]:
    """Given a repo-relative path without extension, probe the file and
    directory-index candidates and return the first that exists (repo-relative,
    POSIX)."""
    rel_no_ext = rel_no_ext.replace('\\', '/').rstrip('/')
    abs_base = os.path.join(repo_path, rel_no_ext.replace('/', os.sep))

    # Exact path already had an extension we recognise.
    if os.path.splitext(rel_no_ext)[1] and os.path.isfile(abs_base):
        return rel_no_ext

    for ext in _EXT_CANDIDATES:
        cand = abs_base + ext
        if os.path.isfile(cand):
            return f'{rel_no_ext}{ext}'

    if os.path.isdir(abs_base):
        for base in _INDEX_BASENAMES:
            cand = os.path.join(abs_base, base)
            if os.path.isfile(cand):
                return f'{rel_no_ext}/{base}'
    return None


def resolve_import(
    specifier: str,
    importer_relpath: str,
    repo_path: str,
    alias_map: Optional[Dict[str, List[str]]] = None,
) -> Optional[str]:
    """Resolve *specifier* imported from *importer_relpath* (repo-relative POSIX).

    Returns the repo-relative POSIX path of the target file, or ``None`` for
    bare specifiers / externals / unresolved paths.
    """
    if not specifier:
        return None
    if alias_map is None:
        alias_map = load_alias_map(repo_path)

    importer_dir = os.path.dirname(importer_relpath.replace('\\', '/'))

    # 1. relative
    if specifier.startswith('.'):
        target = posixpath.normpath(posixpath.join(importer_dir, specifier))
        if target == '..' or target.startswith('../'):
            return None  # escapes the repo root -> confine, no edge
        return _first_existing(repo_path, target)

    # 2. path alias (longest matching prefix wins)
    for key in sorted(alias_map, key=len, reverse=True):
        if specifier == key.rstrip('/') or specifier.startswith(key):
            tail = specifier[len(key):] if specifier.startswith(key) else ''
            tail = tail.lstrip('/')
            for prefix in alias_map[key]:
                cand = posixpath.join(prefix, tail) if prefix else tail
                hit = _first_existing(repo_path, posixpath.normpath(cand))
                if hit:
                    return hit
            return None

    # 3. bare specifier -> external package, no edge in v1
    return None
