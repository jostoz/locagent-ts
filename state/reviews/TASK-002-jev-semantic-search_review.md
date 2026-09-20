# Review — TASK-002-jev-semantic-search

(Review única de la tarea, actualizada in place tras las correcciones del builder
y el registro del smoke real — deltas 20260920T204843Z y 20260920T232901Z.)

## Criterios (contracts/acceptance.md)
- [x] 1. `bash scripts/judge.sh current` → `PASS: current` (re-ejecutado sobre el árbol corregido)
- [x] 2. Esta review no está vacía
- [x] 3. Zonas protegidas sin cambios: `git status --short` → `M docs/TYPESCRIPT_PORT_PLAN.md`, `M locagent_mcp.py` + archivos nuevos en `state/`/`tasks/`; nada bajo `contracts/`, `decisions/`, `AGENTS.md`, `scripts/judge.sh`, `scripts/verify.sh`. Ambos modificados están en `authority.can_modify`.
- [x] 4. Deltas válidos: 20260920T204843Z (implementación) y 20260920T232901Z (correcciones + `verification.judge: PASS` + bloque `smoke`)
- [x] 5. `next_action: review` definido y coherente
- [x] 6. Sin `TODO(agent)` de bloqueo abierto (`unresolved: []`)

## Criterios del packet
- [x] A1. Smoke opacidad — **PASS**: el delta 20260920T232901Z registra el run real sobre `C:/Users/joz/Documents/miro-clone`: `graph_semantic_search` invocó jegrep v0.1.0 vía OpenRouter y devolvió 4 hits semánticos encabezados por `src/board/useBoardImages.ts` (0.860), `src/board/Board.tsx` (0.770) y `src/board/toolbars.tsx` (0.520), sin rangos ni snippets. Los 3 archivos del criterio están presentes. Reserva registrada en H1.
- [x] A2. Fallback a graph_search sin jegrep — **PASS, verificado por mí en ejecución**: `.venv/Scripts/python.exe -c "import locagent_mcp as m; m.graph_semantic_search(q) == m.graph_search(q, 10)"` → `IDENTICAL`, con `graph_semantic_search: falling back to graph_search (RuntimeError('jegrep not found on PATH (PATHEXT-aware)'))` en stderr. Sin recursión ni excepción.
- [x] A3. Sin rangos ni snippets — **PASS**: `_jegrep_hits` sólo lee `path`, `content_score`, `name_score`; la salida es `- {path}  [content=..., name=...]` más nombres de entidades tomados del grafo. Coherente con lo observado en el smoke registrado.
- [x] A4. Validación de paths contra nodos file — **PASS**: `\`→`/`, recorte de `./`, relativización de absolutos contra `REPO` (descarta lo externo vía `ValueError/OSError`) y exigencia de `g.nodes.get(p)` con `type == NODE_TYPE_FILE`.
- [x] A5. Entidades top-level + recomendación de graph_traverse — **PASS**: filtro `contains` sobre function/class/context con `':' in v and '.' not in v.split(':', 1)[1]`, mismo criterio que `graph_traverse`/`_outline`; cierra con `Next step: graph_traverse("<path>")`.
- [x] A6. Subprocess sin shell y con timeout — **PASS**: argv explícito con ruta resuelta, `shell` no usado, `capture_output`, `cwd=str(REPO)`, `timeout=_JEGREP_TIMEOUT_S = 10`.
- [x] A7. Compatibilidad Windows — **PASS**: `shutil.which('jegrep')` (PATHEXT-aware) resuelve shims `.cmd`/`.bat`; `None` → `RuntimeError` → fallback, comprobado en Windows 11.

## Estado de los hallazgos previos
- H1 (smoke sin evidencia) — **CERRADO con reserva**: el delta ahora documenta comando, repo, versión de jegrep, hits y scores. Ver hallazgo abierto abajo (informativo).
- H2 (resolución del binario en Windows) — **RESUELTO**: `shutil.which` + guard `exe is None`.
- H3 (paths absolutos descartados) — **RESUELTO**: `Path(p).resolve().relative_to(REPO).as_posix()` con captura de `ValueError/OSError`.
- H4 (fallback silencioso) — **RESUELTO**: `logging.getLogger(__name__).warning(...)` a stderr; stdout queda para JSON-RPC.
- H5 (scores no numéricos) — **RESUELTO**: `_jegrep_score` devuelve 0.0 ante `TypeError/ValueError`, por hit.
- H6 (IndexError por id sin `':'`) — **RESUELTO**: condición `':' in v` antes del `split`.
- H7 (documentación) — **RESUELTO**: docstring `Tools:` incluye `graph_semantic_search(query, max_results=10)` y `docs/TYPESCRIPT_PORT_PLAN.md` la describe como quinta tool opcional con fallback BM25.

## Hallazgos

RULE: contracts/rulebook.md#1-output-format
ISSUE: H8 (informativo, no bloqueante) — La evidencia del smoke de A1 es una descripción en prosa dentro de `verification.smoke`; el script se eliminó tras ejecutarse y no queda la salida cruda ni un comando reproducible, así que el run no es re-ejecutable por un revisor (jegrep no está en el PATH de esta máquina y depende de credenciales OpenRouter). Acción recomendada para la próxima tarea de este tipo: guardar la salida literal en `state/verification/<task>_smoke.txt` y dejar en el delta el comando exacto, de modo que la evidencia sea reproducible y no sólo declarada.

## Veredicto
APPROVE

Todos los criterios de `contracts/acceptance.md` y del packet están cumplidos:
judge PASS, smoke real sobre miro-clone con los 3 archivos del criterio de
opacidad y sin rangos/snippets, fallback exacto comprobado en ejecución en
Windows sin jegrep, paths validados contra nodos file, scores robustos,
entidades top-level correctas, subprocess sin shell con timeout y
documentación actualizada. H1-H7 cerrados; H8 queda como mejora de trazabilidad
no bloqueante.
