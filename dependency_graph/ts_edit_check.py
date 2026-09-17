"""Behaviour check for the structured-edit layer (`dependency_graph/ts_edit.py`).

The point of the layer is that an edit is refused rather than written when it
would not parse, and that a caller who names something that is not there gets the
list of what is. Both are easy to lose in a refactor and neither is visible from
a successful edit, so each case asserts the *file on disk* did not move.

    python -m dependency_graph.ts_edit_check
    python -m dependency_graph.ts_edit_check --verbose

Exit status 0 when every case holds, 1 otherwise.
"""

import os
import shutil
import sys
import tempfile
from typing import Dict, List, Optional

from dependency_graph.ts_edit import edit_entity, list_entities

BUTTON = """import { cn } from '@/lib/utils';

export const Button = ({ label, onClick }: { label: string; onClick?: () => void }) => (
  <button className={cn('btn')} onClick={onClick}>{label}</button>
);
"""

HOOK = """import { useState } from 'react';

export function useCounter(start: number) {
  const [value, setValue] = useState(start);
  function increment() {
    setValue((current) => current + 1);
  }
  const reset = () => setValue(start);
  return { value, increment, reset };
}
"""

TYPES = """export interface BoardImage {
  id: string
  x: number
  y: number
}

export type Mode = 'a' | 'b'
"""

_FILES = {'src/components/Button.tsx': BUTTON, 'src/hooks/useCounter.ts': HOOK,
          'src/types.ts': TYPES}


def _write(root: str) -> None:
    if os.path.isdir(root):
        shutil.rmtree(root)
    for rel, text in _FILES.items():
        path = os.path.join(root, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)


def _read(root: str, rel: str) -> str:
    with open(os.path.join(root, rel.replace('/', os.sep)), encoding='utf-8') as fh:
        return fh.read()


def run(verbose: bool = False, root: Optional[str] = None) -> int:
    root = root or os.path.join(tempfile.gettempdir(), 'locagent-ts-edit-check')
    failures: List[str] = []

    def check(label: str, condition: bool, detail: str = '') -> None:
        print(f'[{"OK " if condition else "FAIL"}] {label}' + (f'  {detail}' if detail else ''))
        if not condition:
            failures.append(label)

    # 1. replace_in_node: the small, preferred change
    _write(root)
    before = _read(root, 'src/components/Button.tsx')
    out = edit_entity(root, 'src/components/Button.tsx', 'Button', 'replace_in_node',
                      old_str="cn('btn')", new_str="cn('btn', 'primary')")
    after = _read(root, 'src/components/Button.tsx')
    check('replace_in_node escribe el cambio', out['status'] == 'ok' and "cn('btn', 'primary')" in after)
    check('replace_in_node toca una sola línea', after.count('\n') == before.count('\n'),
          f'{before.count(chr(10))} -> {after.count(chr(10))} líneas')

    # 2. substring that is not unique -> refused, file untouched
    _write(root)
    before = _read(root, 'src/hooks/useCounter.ts')
    out = edit_entity(root, 'src/hooks/useCounter.ts', 'useCounter', 'replace_in_node',
                      old_str='setValue', new_str='set')
    check('old_str no único -> rechazado', out['status'] == 'error' and 'veces' in out['message'])
    check('...y el archivo quedó intacto', _read(root, 'src/hooks/useCounter.ts') == before)

    # 3. an edit that would break the syntax is never written
    _write(root)
    before = _read(root, 'src/components/Button.tsx')
    out = edit_entity(root, 'src/components/Button.tsx', 'Button', 'replace_node',
                      replacement='export const Button = ({ label }: { label: string }) => (\n  <button\n);\n')
    check('sintaxis rota -> rechazado', out['status'] == 'error' and out['reason'] == 'sintaxis',
          out.get('message', '')[:90])
    check('...y el archivo quedó intacto', _read(root, 'src/components/Button.tsx') == before)

    # 4. naming something that is not there lists what is
    _write(root)
    out = edit_entity(root, 'src/hooks/useCounter.ts', 'useCunter', 'delete_node')
    names = [c['name'] for c in out.get('candidates') or []]
    check('entidad inexistente -> candidatos', out['status'] == 'error' and 'useCounter' in names,
          str(names))

    # 5. nested entity addressed by its dotted name
    _write(root)
    out = edit_entity(root, 'src/hooks/useCounter.ts', 'useCounter.increment', 'replace_in_node',
                      old_str='current + 1', new_str='current + 2')
    check('entidad anidada por nombre punteado', out['status'] == 'ok'
          and 'current + 2' in _read(root, 'src/hooks/useCounter.ts'), out.get('message', ''))

    # 6. insert_after adds an entity the graph will pick up, at the right indent
    _write(root)
    out = edit_entity(root, 'src/hooks/useCounter.ts', 'useCounter', 'insert_after',
                      replacement='export function useDoubler(start: number) {\n  return start * 2;\n}\n')
    names = [e['name'] for e in list_entities(os.path.join(root, 'src/hooks/useCounter.ts'), 'typescript')]
    check('insert_after añade la entidad', out['status'] == 'ok' and 'useDoubler' in names, str(names))

    # 7. delete_node removes it and reports the entity as gone
    _write(root)
    out = edit_entity(root, 'src/hooks/useCounter.ts', 'useCounter', 'delete_node')
    names = [e['name'] for e in list_entities(os.path.join(root, 'src/hooks/useCounter.ts'), 'typescript')]
    check('delete_node la elimina', out['status'] == 'ok' and out['entity_gone'] is True
          and 'useCounter' not in names, str(names))

    # 8. dry_run reports without writing
    _write(root)
    before = _read(root, 'src/components/Button.tsx')
    out = edit_entity(root, 'src/components/Button.tsx', 'Button', 'delete_node', dry_run=True)
    check('dry_run no escribe', out['status'] == 'ok' and out['dry_run']
          and _read(root, 'src/components/Button.tsx') == before)

    # 9. insert_member: a field inside an interface, at the members' indent
    _write(root)
    out = edit_entity(root, 'src/types.ts', 'BoardImage', 'insert_member',
                      replacement='opacity?: number\n')
    text = _read(root, 'src/types.ts')
    check('insert_member añade el campo con la indentación de los miembros',
          out['status'] == 'ok' and '  opacity?: number' in text, out.get('message', ''))

    # 10. an entity with no member body refuses instead of guessing
    _write(root)
    before = _read(root, 'src/types.ts')
    out = edit_entity(root, 'src/types.ts', 'Mode', 'insert_member', replacement='c: 3')
    check('sin cuerpo de miembros -> rechazado', out['status'] == 'error'
          and 'cuerpo de miembros' in out['message'], out.get('message', '')[:90])
    check('...y el archivo quedó intacto', _read(root, 'src/types.ts') == before)

    # 11. insert_member position='start': lo que el cuerpo ya referencia arriba
    _write(root)
    out = edit_entity(root, 'src/hooks/useCounter.ts', 'useCounter', 'insert_member',
                      replacement='const doubled = value * 2;\n', position='start')
    text = _read(root, 'src/hooks/useCounter.ts')
    decl = next(i for i, l in enumerate(text.splitlines()) if 'export function useCounter' in l)
    first_member = text.splitlines()[decl + 1]
    check("insert_member position='start' entra al comienzo del cuerpo",
          out['status'] == 'ok' and 'doubled' in first_member, first_member.strip()[:60])
    check('...y con la indentación de los miembros', first_member.startswith('  const'), repr(first_member[:22]))

    # 12. un `const` que ya se usa arriba no puede quedar abajo: TS2448
    _write(root)
    with open(os.path.join(root, 'src/hooks/useCounter.ts'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write("""export function useCounter(start: number) {
  const api = { reset };
  return api;
}
""")
    out = edit_entity(root, 'src/hooks/useCounter.ts', 'useCounter', 'insert_member',
                      replacement='const reset = () => 1;\n')
    text = _read(root, 'src/hooks/useCounter.ts')
    decl = next(i for i, l in enumerate(text.splitlines()) if 'export function useCounter' in l)
    check('`const` usado arriba se coloca al inicio (no debajo del uso)',
          out['status'] == 'ok' and 'reset' in text.splitlines()[decl + 1]
          and 'no se hoistea' in out.get('detail', ''), out.get('detail', '')[:80])

    shutil.rmtree(root, ignore_errors=True)
    total = 15
    print(f'edit_check: {total - failures.__len__()}/{total} casos OK')
    return 1 if failures else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    return run(verbose='--verbose' in args or '-v' in args)


if __name__ == '__main__':
    raise SystemExit(main())
