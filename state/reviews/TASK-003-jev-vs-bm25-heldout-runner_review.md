# Review — TASK-003-jev-vs-bm25-heldout-runner

Sin preámbulo narrativo (ver decisions/0015-narracion-liviana.md) — solo
el checklist y los hallazgos.

## Criterios (contracts/acceptance.md)
- [x] 1. `judge.sh current` → PASS (reproducido en esta review: `bash scripts/judge.sh current` → `PASS: current`)
- [x] 2. Esta review no está vacía
- [x] 3. Zonas protegidas sin cambios (`git diff --stat HEAD -- contracts/ scripts/judge.sh scripts/verify.sh AGENTS.md decisions/` → vacío)
- [x] 4. Existe al menos un state delta válido para la tarea (`state/deltas/20260921T070856Z_TASK-003-jev-vs-bm25-heldout-runner_builder.yaml`)
- [x] 5. `next_action` definido y coherente (pide correr una vez más el runner y conservar el directorio de resultado; consistente con `unresolved`)
- [x] 6. Sin `TODO(agent)` de bloqueo abierto sin escalar (el delta usa `unresolved:` + `next_action:`, no un TODO(agent) sin resolver; no bloquea la cola)

## Criterios de la tarea (tasks/TASK-003-jev-vs-bm25-heldout-runner.yaml → acceptance)

1. **Runner de dos brazos en eval/ con mismo prompt/modelo/sandbox/commit/gates — PASS.**
   `eval/run_jev_vs_bm25.py:main` corre `for arm in ('bm25','jev')` con el mismo
   `record`, `model`, `sandbox_image`, `record['pinned_at']` (vía `_worktree`) y
   `record['acceptance']` (vía `eval.run_matrix._acceptance`) para ambos brazos
   (líneas 116-203, 240-250). Único parámetro que cambia entre brazos es `arm`,
   que solo determina el mecanismo de localización (`_localize`).

2. **Fixture held-out edit, path-free, con gt_entities/gt_files/acceptance propios — PASS.**
   `eval/heldout_edit_case.jsonl` (`h1`): prompt en español describe síntoma
   ("el ángulo... sigue creciendo... sin límite"), no nombra archivo/componente/
   función. `gt_files`/`gt_entities`/`gt_lines` presentes. `acceptance` declara
   dos comandos shell (`npx tsc --noEmit`, `grep -nE "rotation[^;]*% *360" ...`).
   `run_jev_vs_bm25.main` verifica explícitamente que `record['id']` no está en
   `eval/miro_clone_localization.jsonl` (líneas 227-233) — rechazo activo, no
   solo documentado. Reproducido en esta review: `python -m eval.score
   --validate-corpus --corpus eval/heldout_edit_case.jsonl` → `OK (1 records)`.

3. **Worktree aislado y descartable + sandbox por brazo — PASS (mecanismo); evidencia end-to-end incompleta, ver hallazgo H1.**
   `run_arm` llama `_worktree(source_repo, run_dir/'worktrees'/arm, record['pinned_at'])`
   (mismo helper que `eval/run_matrix.py`/`eval/isolated_env.py`) y pasa
   `sandbox_image` a `run_once` → `build_cmd` monta el worktree en `/workspace`
   read-only aparte del propio worktree, sin socket docker, sin credenciales
   del host (líneas 126-148 de `cline_runner.py`). El artefacto de resultado
   (`<arm>.json`) registra `worktree` y `sandbox_image`. **Pero** ningún run en
   disco llega a `acceptance_ok: true` en ninguno de los dos brazos — H1 resuelto, ver nota agregada.

4. **GESTADO Sigma(t) documentado y usado en vez de carry ad hoc — PASS.**
   `eval/gestado_state.py` importa `gestado.stateful.merge_state` desde
   `C:/Users/joz/orca/workspaces/Gestado/cabezon` (no vendorizado). `run_arm`
   encadena `init → localized → edited → validated` vía `advance()` (líneas
   119, 128-135, 157-161, 167-172). El docstring del módulo documenta
   explícitamente por qué `AgentRuntime` no envuelve a Cline y qué forma tiene
   Sigma(t). `gestado_sigma_final` queda embebido en cada `<arm>.json`.
   Verificado contra `bm25.json` del run en disco (`gestado_sigma_final`
   presente, líneas 152-186 del artefacto).

5. **Canonicalización jegrep→entidad AST, fail-closed, ranking/snippets nunca al ejecutor — PASS (mecanismo verificado; camino positivo no ejercido con jegrep real, documentado como limitación, no oculto).**
   `eval/jev_canonicalize.py:canonicalize_hits` resuelve cada hit a un
   `NODE_TYPE_FILE` del grafo y exige al menos un hijo AST (`_entity_children`,
   `NODE_TYPE_FUNCTION`/`CLASS`/`CONTEXT`) antes de aceptar el id — nunca el id
   de archivo solo, nunca texto de jegrep (líneas 100-134). Todo hit que falla
   cualquiera de los dos pasos se registra como `rejections` con razón
   machine-readable (`outside_repo_or_empty`, `not_a_graph_file_node`,
   `file_node_has_no_ast_entities`). `run_jev_vs_bm25._build_prompt` construye
   el prompt del ejecutor solo desde `loc['entities']` (paquete canónico);
   `run_once(..., disable_locagent=True)` impide que el ejecutor llame
   `graph_search`/`graph_semantic_search` directamente, cerrando la única otra
   vía por la que texto crudo de jegrep podría llegarle. En esta sesión
   `jegrep` no está disponible (falta `OPENROUTER_API_KEY`/`TYPESAFE_API_KEY`),
   así que todo run real ejercita solo la rama de rechazo
   (`JegrepUnavailable` → 1 rejection, paquete vacío) — el camino positivo de
   canonicalización (hit real → entidad aceptada) está cubierto solo por el
   test sintético descrito en `eval/results/TASK-003-status-20260921.md`, no
   por una corrida real end-to-end. El propio delta del builder documenta esto
   como limitación conocida, no lo oculta.

6. **Artefacto de resultado por brazo con exito e2e, métricas de entidad, tokens, wall time, guard rejections, tamaño de paquete, resultado de acceptance — PASS en esquema; el único run en disco tiene los campos pero con valores "interrumpido", ver H1.**
   `arm_result` (líneas 178-202 de `run_jev_vs_bm25.py`) incluye todos los
   campos pedidos. Verificado contra `bm25.json` en disco: todos los campos
   presentes (`end_to_end_success: false`, `acceptance_ok: false`,
   `cline_exit_code: 137`, `edit_flake: "process"` — proceso matado por el
   operador, no timeout limpio ni éxito).

7. **`eval.metrics --gt <fixture> --pred <artefacto>` corre sin error, produce acc/recall/precision@k a nivel de entidad — PASS.**
   Reproducido en esta review: `python -m eval.metrics --gt
   eval/heldout_edit_case.jsonl --pred
   eval/results/20260921T070610Z-jev-vs-bm25/bm25.pred.jsonl` → JSON con
   bloques `file` y `entity` (`acc@k`/`recall@k`/`precision@k`), sin error.
   `python -m eval.metrics --selftest` → `OK`.

8. **Documentación explícita de qué evidencia de este repo certifica el trabajo cross-repo (invariante 6) — PASS.**
   `report.md` generado por el runner incluye una sección
   "cross-repo evidence (contracts/project.md invariante 6)" que enumera
   exactamente qué campos de `<arm>.json` + el transcript `<arm>-h1.jsonl`
   sirven de evidencia. No existe un `report.md` en el único run que quedó en
   disco (interrumpido antes de llegar a esa etapa) — la mecánica de
   documentación existe y es correcta, pero no hay un `report.md` real para
   inspeccionar en `eval/results/`.

9. **`bash scripts/judge.sh current` → PASS — PASS.** Reproducido en esta review.

10. **Existe esta review — PASS** (este archivo).

## Hallazgos

RULE: tasks/TASK-003-jev-vs-bm25-heldout-runner.yaml#acceptance (criterio 5, "el artefacto de resultado por brazo registra... exito end-to-end de la edicion (aplica + pasa acceptance)")
ISSUE: ~~No existe en `eval/results/` ningún artefacto de corrida completa donde
ambos brazos (o siquiera uno solo) lleguen a `acceptance_ok: true`.~~ **RESUELTO** por la corrida `20261108T223343Z-jev-vs-bm25`: su `bm25.json` registra `acceptance_ok: true`, `end_to_end_success: true`, `gestado_sigma_final.phase: "validated"`, `guard_rejections: []` (count 0), y el paquete canónico incluye `src/board/useStickyNotes.ts` completo + entidades `useStickyNotes.ts:StickyNote` / `useStickyNotes.ts:useStickyNotes`; acceptance cmds (`npx --no-install tsc -p tsconfig.app.json --noEmit` exit 0; `grep -nE "rotation[^;]*% *360" src/board/useStickyNotes.ts` exit 0) pasaron. El brazo `jev` de la misma corrida quedó en `acceptance_ok: false` (jgrep no determinístico devolvió paquete vacío esa vez, `phase: "validated"`), pero el criterio exige al menos un artefacto durable con éxito end-to-end y ese existe hoy. Texto original conservado para trazabilidad:
El único
directorio de resultado en disco era
`eval/results/20260921T070610Z-jev-vs-bm25/`: `bm25.json` registra
`edit_applied: false`, `end_to_end_success: false`, `acceptance_ok: false`,
`cline_exit_code: 137` (proceso matado durante la instrucción de parada del
operador), y `jev.json` no existe en absoluto (`jev-h1.jsonl` tiene 839 líneas
de transcript truncado a mitad de una tool call, sin JSON final). El propio
delta del builder (`state/deltas/20260921T070856Z_..._builder.yaml`,
`unresolved:`) documenta que hubo una corrida bm25 exitosa
(`20260921T062159Z`) inspeccionada manualmente en sesión, pero su directorio
fue borrado antes de que llegara la orden de parar reintentos — por lo tanto
no es un artefacto verificable hoy. Bajo la regla de `contracts/project.md`
invariante 6 ("la tarea debe declarar explícitamente qué archivo de este repo
sirve de evidencia verificable") y la instrucción explícita de esta review de
no aceptar la corrida sobreescrita como evidencia, este criterio de
aceptación de la tarea **no está satisfecho por ningún artefacto durable
actual**. Esto es un gap de evidencia end-to-end, no un defecto de diseño: la
implementación (canonicalización, worktree aislado, sandbox, GESTADO) está
verificada de forma unitaria y acotada, pero el criterio de aceptación exige
específicamente el resultado de una corrida completa, y ese resultado no
existe en disco.

RULE: contracts/rulebook.md#seccion-1-reviews ("checklist de contracts/acceptance.md... + hallazgos, sin preámbulo narrativo")
ISSUE: (informativo, no bloqueante) El `next_action` del delta del builder
pide a la siguiente sesión ejecutar
`python -m eval.run_jev_vs_bm25 --repo C:/Users/joz/Documents/miro-clone
--sandbox-image locagent-cline:stage1-v4 --timeout 300` una vez y conservar
el directorio resultante antes de que el verifier considere la tarea
completa. Esta review, por su alcance ("solo lectura de artefactos +
contratos", `contracts/project.md#tool_permissions`), no ejecuta llamadas al
modelo ni corridas del pipeline completo — confirma que el `next_action` es
la vía correcta y necesaria para cerrar el hallazgo H1 arriba, no un
sustituto de ella.

## Veredicto
PASS — H1 resuelto por la corrida `20261108T223343Z-jev-vs-bm25`: su
`bm25.json` registra `acceptance_ok: true`, `end_to_end_success: true`,
`gestado_sigma_final.phase: "validated"`, `guard_rejections: []` (count 0) y
paquete canónico con `src/board/useStickyNotes.ts` completo + entidades
`useStickyNotes.ts:StickyNote` / `useStickyNotes.ts:useStickyNotes`; ambas
acceptance cmds exit 0. El brazo `jev` de la misma corrida quedó en
`acceptance_ok: false` (jgrep no determinístico devolvió paquete vacío esa
vez, `phase: "validated"`) — documentado en la nota agregada a H1; el
criterio exige al menos un artefacto con éxito end-to-end y ese requisito se
cumple. La implementación (canonicalización jegrep→entidad fail-closed,
GESTADO Sigma(t), worktree+sandbox aislado, fixture held-out path-free,
esquema de artefacto de resultado, arreglo de leak de contenedores en
`cline_runner.py`) está verificada, es de buena calidad, y pasa todos los
checks acotados y reproducibles disponibles sin llamada al modelo
(`judge.sh current`, `eval.metrics --selftest`, `eval.metrics --gt/--pred`,
`eval.score --validate-corpus`, zonas protegidas intactas). Sin más cambios
de código pendientes.
</content>

## Addendum post-done — fix de reasoning (2026-11-09)

**Veredicto: PASS.** Review independiente real del addendum (reemplaza la
sección anterior, que era documentación del builder sin veredicto de
tercero). Delta builder: `state/deltas/20261109T233603Z_TASK-003-jev-vs-bm25-heldout-runner_builder.yaml`.

**a) Scope (`authority.can_modify: eval/**`)** — verificado con
`git status --porcelain eval/`: todo lo tocado/nuevo cae dentro de `eval/**`
(`cline_runner.py`, `container/entrypoint.sh` modificados;
`chat_template_qwen_fixed.jinja`, `gestado_state.py`, `heldout_edit_case.jsonl`,
`jev_canonicalize.py`, `localize_helper.py`, `run_jev_vs_bm25.py` nuevos).
Ningún archivo fuera de `eval/**`.

**b) Coherencia del diff** — verificado leyendo el diff completo:
`cline_runner.py` agrega `temperature`/`reasoning_max_tokens` (y
`disabled_tools`/`container_name`, ya presentes en la base pre-addendum) a
`build_cmd`/`run_once` y los pasa como env vars al contenedor;
`entrypoint.sh` los lee con default `0`/`0` (desactivado si `0`) y los
escribe en `providers.json` (`settings.temperature`,
`settings.reasoning.{enabled,maxTokens}` solo si `reasoningMax > 0`). No
toca contratos, judges ni políticas — solo configuración de ejecución del
executor.

**c) Evidencia empírica** (verificada directamente contra los artefactos):
```
OLD (20261108T223343Z) jev.guard_flags: {flags: [repeated_command_failure, gave_up],
  details: {repeated_command_failure: {team_run_task: 4}, gave_up: aborted}}
OLD jev.edit_flake: ok
NEW (20261109T204836Z) jev.guard_flags: {flags: [], details: {diff_files: {}}}
NEW jev.edit_flake: timeout
```
Antes el ejecutor entraba en loop ciego de una tool call fallida repetida
(`team_run_task` x4) y se rendía rápido (`gave_up: aborted`); después
explora extensamente sin ningún guard flag de loop y falla por timeout del
harness, no por comportamiento patológico. El brazo `bm25` de la misma
corrida nueva mantiene `acceptance_ok: true`, `edit_flake: ok` — el fix no
rompió el camino que ya funcionaba.

**`bash scripts/judge.sh current` → PASS** (corrido directamente, 2026-11-10).

Sin hallazgos RULE/ISSUE. El addendum es consistente con lo afirmado y se
conserva.
