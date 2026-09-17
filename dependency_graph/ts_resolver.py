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
from typing import Dict, List, Optional

# Extension probe order when a specifier has no extension.
_EXT_CANDIDATES = ('.ts', '.tsx', '.d.ts', '.js', '.jsx', '.mts', '.cts', '.mjs', '.cjs')
_INDEX_BASENAMES = tuple(f'index{ext}' for ext in _EXT_CANDIDATES)

# tsconfig files to consult at the repo root, most specific first.
_TSCONFIG_NAMES = ('tsconfig.json', 'tsconfig.app.json', 'tsconfig.base.json')


def _strip_jsonc(text: str) -> str:
    """Strip ``//`` and ``/* */`` comments and trailing commas so a tsconfig
    (JSONC) can go through ``json.loads``.

    String-aware on purpose: a naive regex eats the ``/*`` inside the glob
    ``"**/*.ts"`` -- which every Next.js tsconfig has in ``include`` -- and
    silently truncates the file, losing ``compilerOptions.paths`` and with it
    every ``@/`` import (measured: DeskcommCRM resolved 0 aliases that way).
    """
    out: List[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] not in '\r\n':
                i += 1
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '*':
            end = text.find('*/', i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(ch)
        i += 1

    # trailing commas: keep string contents untouched too
    cleaned: List[str] = []
    i, n = 0, len(out)
    while i < n:
        ch = out[i]
        if ch == '"':
            j = i + 1
            while j < n and not (out[j] == '"' and out[j - 1] != '\\'):
                j += 1
            cleaned.extend(out[i:j + 1])
            i = j + 1
            continue
        if ch == ',':
            j = i + 1
            while j < n and out[j] in ' \t\r\n':
                j += 1
            if j < n and out[j] in '}]':
                i += 1
                continue
        cleaned.append(ch)
        i += 1
    return ''.join(cleaned)


def _read_jsonc(path: str) -> Optional[dict]:
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.loads(_strip_jsonc(fh.read()))
    except (ValueError, OSError):
        return None


_EXTENDS_MAX_DEPTH = 5


def _load_tsconfig(path: str, _depth: int = 0, _seen: Optional[set] = None) -> Optional[dict]:
    """Parse one tsconfig with its ``extends`` chain merged in (parent first, so
    the child's ``compilerOptions`` win). Returns ``None`` when it is missing or
    unparseable.

    ``compilerOptions`` entries carry the directory of the config that declared
    them (``_dir``), because a child's ``baseUrl`` is relative to the child while
    an inherited one stays relative to the parent -- path aliases break if the
    two are conflated."""
    if _depth > _EXTENDS_MAX_DEPTH:
        return None
    _seen = _seen or set()
    real = os.path.normcase(os.path.abspath(path))
    if real in _seen:
        return None
    _seen.add(real)

    cfg = _read_jsonc(path)
    if cfg is None:
        return None

    here = os.path.dirname(path)
    merged: Dict[str, object] = {}
    inherited_paths = inherited_base = None
    for key, value in (cfg.get('compilerOptions') or {}).items():
        merged[key] = value
    if 'paths' in merged:
        inherited_paths = (merged['paths'], here)
    if 'baseUrl' in merged:
        inherited_base = (merged['baseUrl'], here)

    parent_ref = cfg.get('extends')
    if isinstance(parent_ref, str) and parent_ref:
        if parent_ref.startswith('.'):
            base = os.path.join(here, parent_ref.replace('/', os.sep))
        else:  # package-style extends (e.g. "next/tsconfig.json") -- unresolved
            base = None
        candidates = [base, base + '.json'] if base else []
        for cand in candidates:
            if cand and os.path.isfile(cand):
                parent = _load_tsconfig(cand, _depth + 1, _seen)
                if parent:
                    p_opts = parent.get('compilerOptions') or {}
                    merged = {**p_opts, **merged}
                    inherited_paths = parent.get('_paths', (None, None))
                    inherited_base = parent.get('_base', (None, None))
                    if 'paths' in (cfg.get('compilerOptions') or {}):
                        inherited_paths = (cfg['compilerOptions']['paths'], here)
                    if 'baseUrl' in (cfg.get('compilerOptions') or {}):
                        inherited_base = (cfg['compilerOptions']['baseUrl'], here)
                break

    return {'compilerOptions': merged,
            '_paths': inherited_paths or (merged.get('paths'), here),
            '_base': inherited_base or (merged.get('baseUrl'), here)}


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
        cfg = _load_tsconfig(cfg_path)
        if not cfg:
            continue
        paths, paths_dir = cfg.get('_paths') or (None, None)
        base_url, base_dir = cfg.get('_base') or (None, None)
        if not paths:
            continue

        # baseUrl resolves against its own config's dir; default is the dir of
        # the config that declared `paths` (TS: paths are relative to baseUrl,
        # which itself defaults to the tsconfig's directory).
        def _rel_dir(path: str) -> str:
            rel = os.path.relpath(path, repo_path).replace(os.sep, '/')
            return '' if rel == '.' else rel

        base_prefix = _rel_dir(base_dir or paths_dir or repo_path)
        if base_url:
            base_prefix = posixpath.normpath(
                posixpath.join(base_prefix, base_url.replace('\\', '/')))
            if base_prefix == '.':
                base_prefix = ''

        alias_map: Dict[str, List[str]] = {}
        for pattern, targets in paths.items():
            # "@/*" -> key "@/", "@app" -> key "@app"
            key = pattern[:-1] if pattern.endswith('*') else pattern
            resolved: List[str] = []
            for target in targets:
                # "./src/*" -> "./src", "src/lib" -> "src/lib"
                t = target[:-1] if target.endswith('*') else target
                joined = posixpath.normpath(posixpath.join(base_prefix, t))
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
