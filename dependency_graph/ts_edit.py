"""Structured edits over named AST entities -- the TypeScript replacement for a
code-as-action (CodeAct) space.

A code-as-action agent mutates a file by emitting a program that writes bytes.
This module mutates a file by naming an *entity* the graph already resolved
(``path:Entity`` or ``path:Clase.metodo``) and stating an operation on it, with
the syntax check performed in the file's own grammar and the write refused if the
result does not parse at least as well as the original.

The contract is adapted from CodeStruct (ACL 2026, Amazon; CC-BY-NC-4.0) --
`readCode`/`editCode` over named AST entities, with errors that teach -- but the
language reality is TypeScript/React: ranges come from the graph builder's own
tree-sitter pass with the grammars this fork uses (``tsx`` for ``.tsx``), which
is what makes it work on components at all.

Operations, and when each is the right one:

    replace_in_node   one unique substring inside the entity -- small changes
    insert_member     a new member inside the entity's body (a field of an
                      interface, a method of a class, a statement in a function).
                      ``position='start'`` puts it at the top of the body. When the
                      member declares a ``const``/``let`` whose name is already used
                      above the insertion point (a hook's ``return {...}`` is the
                      usual case) the member is placed at the top *and the answer
                      says so*: a ``const`` is not hoisted, "used before
                      declaration" is a semantic error the syntax gate cannot see,
                      and an agent with no compiler access cannot discover it
    replace_node      the whole entity: new function, rewritten body
    insert_before     a new entity above the target, at the target's indent
    insert_after      a new entity below the target
    delete_node       the whole entity, including its leading comment block

Prefer ``replace_in_node`` for anything small: replacing a whole entity rewrites
code the caller did not read, and the diff stops being reviewable.

Pure text + AST: this module never touches the graph. Addressing, candidate
lists and the wiring report belong to the caller (``locagent_mcp.graph_edit``).
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from dependency_graph.ts_build_graph import (
    GRAMMAR_BY_EXT,
    analyze_ts_file,
    get_parser,
    load_source,
)

OPERATIONS = ('replace_in_node', 'insert_member', 'replace_node', 'insert_before',
              'insert_after', 'delete_node')

_MAX_LISTED_CANDIDATES = 25


def _syntax_errors(grammar: str, code: str) -> List[Tuple[int, str]]:
    """Every ERROR / missing node in *code*, as ``(line, node type)``."""
    tree = get_parser(grammar).parse(bytes(code, 'utf8'))
    out: List[Tuple[int, str]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == 'ERROR' or node.is_missing:
            out.append((node.start_point[0] + 1, node.type))
        stack.extend(node.children)
    return sorted(out)


def _expand_tabs(text: str) -> str:
    return text.expandtabs(4)


def _entity_indent(lines: List[str], start: int, end: int) -> str:
    """Leading whitespace of the entity's first non-blank line."""
    for line in lines[start - 1:end]:
        if line.strip():
            return line[:len(line) - len(line.lstrip())]
    return ''


def _reindent(block: str, target_indent: str) -> List[str]:
    """Shift *block* so its minimum indentation equals *target_indent*, keeping
    the block's internal structure. Tabs are expanded first: an LLM that emits a
    tab-indented method inside a space-indented class produces a file that parses
    but reads as garbage."""
    block = _expand_tabs(block)
    lines = block.split('\n')
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return []
    base = min((len(l) - len(l.lstrip()) for l in lines if l.strip()), default=0)
    out = []
    for line in lines:
        if not line.strip():
            out.append('')
        else:
            out.append(target_indent + line[base:].rstrip())
    return out


def list_entities(abs_path: str, grammar: str) -> List[dict]:
    """The file's entities, as ``{name, type, start_line, end_line}`` -- used to
    teach a caller that named something that is not there."""
    entities, _imports = analyze_ts_file(abs_path, grammar)
    return [{'name': e['name'], 'type': e['type'], 'ts_kind': e.get('ts_kind'),
             'start_line': e['start_line'], 'end_line': e['end_line'],
             'member_body': e.get('member_body')}
            for e in entities]


def find_entity(abs_path: str, grammar: str, name_path: str) -> List[dict]:
    """Every entity whose dotted name equals *name_path* (usually 0 or 1)."""
    wanted = name_path.strip()
    return [e for e in list_entities(abs_path, grammar)
            if e['name'] == wanted or e['name'].split('.')[-1] == wanted]


def _apply(lines: List[str], entity: dict, operation: str, replacement: str,
           old_str: str, new_str: str,
           position: str = 'end') -> Optional[Tuple[List[str], str]]:
    """Return ``(new_lines, detail)`` or ``None`` when the operation is invalid."""
    start, end = entity['start_line'], entity['end_line']
    indent = _entity_indent(lines, start, end)

    if operation == 'replace_in_node':
        if not old_str or new_str is None:
            return None
        block = '\n'.join(lines[start - 1:end])
        count = block.count(old_str)
        if count != 1:
            raise ValueError(f'old_str aparece {count} veces dentro de {entity["name"]}; '
                             'tiene que aparecer exactamente una vez')
        if old_str == new_str:
            raise ValueError('old_str y new_str son iguales')
        block = block.replace(old_str, new_str, 1)
        return lines[:start - 1] + block.split('\n') + lines[end:], 'sustitución única en el nodo'

    if operation == 'insert_member':
        if not replacement or not replacement.strip():
            raise ValueError('replacement vacío')
        body = entity.get('member_body')
        if not body:
            raise ValueError(f'{entity["name"]} no tiene cuerpo de miembros donde insertar '
                             '(¿es una función de expresión o un alias de unión?)')
        body_start, body_end = body
        members = [l for l in lines[body_start:body_end - 1] if l.strip()]
        if members:
            member_indent = _entity_indent(lines, body_start + 1, body_end) or (indent + '    ')
        else:
            member_indent = indent + '    '
        new_members = _reindent(replacement, member_indent)
        if not new_members:
            raise ValueError('replacement no contiene código')
        # Un `const`/`let` no se hoistea: si el nombre ya se usa más arriba en el
        # archivo (el `return {...}` de un hook es el caso típico), insertarlo al
        # final produce TS2448 -- un error semántico que el chequeo de sintaxis no
        # puede ver y que el agente, sin compilador, no puede descubrir. La
        # colocación se decide acá y se informa, en vez de depender de que el
        # llamante lo recuerde.
        hoisted_note = ''
        if not position.startswith('start'):
            declared = re.search(r'\b(?:const|let)\s+([A-Za-z_$][\w$]*)', replacement)
            if declared:
                name = declared.group(1)
                above = '\n'.join(lines[:body_end - 1])
                if re.search(r'\b' + re.escape(name) + r'\b', above):
                    position = 'start'
                    hoisted_note = (f' (colocada al inicio: `{name}` se usa más arriba y '
                                    '`const` no se hoistea)')
        if position == 'start':      # al comienzo del cuerpo, tras la llave de apertura
            return (lines[:body_start] + new_members + lines[body_start:],
                    f'{len(new_members)} línea(s) insertadas al inicio del cuerpo{hoisted_note}')
        # justo antes de la llave de cierre del cuerpo (por defecto)
        return (lines[:body_end - 1] + new_members + lines[body_end - 1:],
                f'{len(new_members)} línea(s) insertadas en el cuerpo')

    if operation == 'replace_node':
        if not replacement or not replacement.strip():
            raise ValueError('replacement vacío')
        body = _reindent(replacement, indent)
        if not body:
            raise ValueError('replacement no contiene código')
        return lines[:start - 1] + body + lines[end:], f'{end - start + 1} líneas reemplazadas'

    if operation in ('insert_before', 'insert_after'):
        if not replacement or not replacement.strip():
            raise ValueError('replacement vacío')
        body = _reindent(replacement, indent)
        if not body:
            raise ValueError('replacement no contiene código')
        if operation == 'insert_before':
            return lines[:start - 1] + body + [''] + lines[start - 1:], 'insertado antes'
        return lines[:end] + [''] + body + lines[end:], 'insertado después'

    if operation == 'delete_node':
        tail = lines[end:]
        if tail and not tail[0].strip():
            tail = tail[1:]
        return lines[:start - 1] + tail, f'{end - start + 1} líneas eliminadas'

    return None


def edit_entity(repo_path: str, rel_file: str, name_path: str, operation: str,
                replacement: str = '', old_str: str = '', new_str: str = '',
                dry_run: bool = False, position: str = 'end') -> Dict[str, object]:
    """Apply *operation* to the entity *name_path* inside *rel_file*.

    Returns ``{'status': 'ok'|'error', ...}``. On ``error`` the file is
    untouched: either the entity is not there (``candidates`` carries the file's
    entities so the caller can correct the name) or the edit would make the file
    parse worse than it did, which is never written.
    """
    abs_path = os.path.join(repo_path, rel_file.replace('/', os.sep))
    grammar = GRAMMAR_BY_EXT.get(os.path.splitext(rel_file)[1])
    if grammar is None:
        return {'status': 'error', 'reason': 'lenguaje no soportado',
                'message': f'{rel_file}: extensión sin gramática'}
    if operation not in OPERATIONS:
        return {'status': 'error', 'reason': 'operación desconocida',
                'message': f'operación {operation!r}; válidas: {", ".join(OPERATIONS)}'}
    code = load_source(abs_path)
    if code is None:
        return {'status': 'error', 'reason': 'ilegible',
                'message': f'no se pudo leer {rel_file}'}

    matches = find_entity(abs_path, grammar, name_path)
    if not matches:
        return {'status': 'error', 'reason': 'entidad inexistente',
                'message': f'{rel_file} no define {name_path!r}. NO repitas el mismo '
                           f'comando: usá uno de los selectores listados.',
                'candidates': list_entities(abs_path, grammar)[:_MAX_LISTED_CANDIDATES]}
    if len(matches) > 1:
        return {'status': 'error', 'reason': 'selector ambiguo',
                'message': f'{name_path!r} aparece {len(matches)} veces en {rel_file}; '
                           'calificá con la clase contenedora',
                'candidates': matches}

    entity = matches[0]
    lines = code.split('\n')
    try:
        applied = _apply(lines, entity, operation, replacement, old_str, new_str,
                         position=position)
    except ValueError as exc:
        return {'status': 'error', 'reason': 'operación inválida', 'message': str(exc),
                'candidates': []}
    if applied is None:
        return {'status': 'error', 'reason': 'operación inválida',
                'message': f'operación {operation!r} incompleta (¿falta replacement?)'}
    new_lines, detail = applied
    new_code = '\n'.join(new_lines)

    before_errors = _syntax_errors(grammar, code)
    after_errors = _syntax_errors(grammar, new_code)
    if len(after_errors) > len(before_errors):
        first = after_errors[0]
        return {'status': 'error', 'reason': 'sintaxis',
                'message': f'la edición introduciría {len(after_errors) - len(before_errors)} '
                           f'error(es) de sintaxis (primero en la línea {first[0]}). '
                           'No se escribió nada.',
                'syntax_errors': after_errors[:5]}

    names_after: List[str] = []
    if not dry_run:
        tmp = abs_path + '.locagent-tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(new_code)
        os.replace(tmp, abs_path)
        names_after = [e['name'] for e in list_entities(abs_path, grammar)]
    else:
        probe = abs_path + '.locagent-dry'
        try:
            with open(probe, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(new_code)
            names_after = [e['name'] for e in list_entities(probe, grammar)]
        finally:
            if os.path.exists(probe):
                os.remove(probe)

    gone = not any(n == entity['name'] or n.split('.')[-1] == name_path.split('.')[-1]
                   for n in names_after)

    return {'status': 'ok', 'file': rel_file, 'entity': entity['name'],
            'operation': operation, 'detail': detail,
            'lines_before': entity['end_line'] - entity['start_line'] + 1,
            'lines_total': len(new_lines),
            'entity_gone': gone,
            'dry_run': dry_run,
            'syntax_errors_before': len(before_errors),
            'syntax_errors_after': len(after_errors)}
