# Project Contract — locagent-ts

## mission
Port de LocAgent a TypeScript/TSX (tree-sitter) + servidor MCP para Cline
(`locagent_mcp.py`: `graph_search`/`graph_get`/`graph_traverse`/`graph_map`
sobre un grafo de código + índice BM25). Trabajo activo (rama
`feat/staged-orchestrator`, ver `docs/TYPESCRIPT_PORT_PLAN.md`): un
experimento de orquestador escalonado — ¿un planificador fuerte (DeepSeek)
troceando la tarea en pasos verificables mejora a un ejecutor local chico
(`qwen/qwen3.5-9b` vía LM Studio + Cline CLI) frente a dejarlo resolver la
tarea completa solo? El protocolo PAIOS (este bootstrap) es la capa de
verificación de esa segunda mitad del experimento — ver
`paios-bootstrap/decisions/0018-integracion-locagent-ts-stage2.md` para el
razonamiento completo detrás de esta adopción.

## source_hierarchy
```
contracts/            (autoridad máxima de reglas)
state/                (estado operativo actual: colas, deltas, reviews, baselines)
tasks/                (trabajo pendiente y definición)
projections/          (vistas derivadas, pueden quedar obsoletas)
dependency_graph/      (extractor tree-sitter + grafo + BM25 — el producto central)
plugins/               (utilidades de localización portadas de LocAgent)
eval/                  (harness del experimento Stage 1/2: corpus, métricas, planner, cline_runner, guard)
docs/                  (TYPESCRIPT_PORT_PLAN.md — el plan vivo del proyecto)
locagent_mcp.py         (servidor MCP, entrypoint)
requirements*.txt       (deps; requirements.txt sirve al harness; requirements-ts.txt queda reservado para el port TypeScript)
```
No hay `decisions/` propio todavía en este repo — las decisiones
arquitectónicas de esta integración viven en `paios-bootstrap/decisions/`
(cross-repo, ver invariante 6 abajo) hasta que este repo acumule las suyas
propias.

## canonical_boundaries
- Estado canónico: Git history + `state/deltas/` + `contracts/`
- Proyecciones derivadas: `docs/TYPESCRIPT_PORT_PLAN.md` (parcialmente —
  también es documentación de fondo, no solo proyección), `projections/status.md`
- Las proyecciones pueden regenerarse; el estado canónico es la autoridad.

## invariants
1. Ningún agente modifica `contracts/` ni `scripts/judge.sh`/`scripts/verify.sh`
   sin aprobación humana.
2. Todo trabajo deja un state delta.
3. El judge determinista debe poder fallar (prueba `known_good` / `intentionally_broken`).
4. Un review honesto por tarea antes de aceptación final, no dual review
   simulado (mismo razonamiento que `paios-bootstrap/decisions/0003-single-honest-review.md`).
5. Secretos nunca entran al repo ni a los prompts — en particular,
   `DEEPSEEK_API_KEY` se lee solo de la variable de entorno, nunca se copia
   de `~/.cline/data/settings/providers.json`.
6. **`authority.can_modify` es `git diff` contra el HEAD de *este* repo**
   (mecanismo de `paios-bootstrap/decisions/0006-task-authority-scope.md`,
   copiado sin cambios). Una tarea cuyo entregable real edita otro repo
   (p.ej. `C:/Users/joz/Documents/miro-clone`, usado como corpus de prueba
   por `eval/`) no puede depender de `verify.sh` para detectar esos cambios
   — `authority.can_modify` no ve nada fuera de este working tree. Para esas
   tareas, la tarea debe declarar explícitamente en `acceptance` qué archivo
   de este repo (script, reporte, log) sirve de evidencia verificable de que
   el trabajo externo se hizo, o la tarea se considera fuera del alcance
   mecánico de `verify.sh` y necesita revisión humana adicional.

## schemas
- Tareas: `tasks/*.yaml`
- Deltas: `state/deltas/*.yaml`
- Reviews: `state/reviews/*.md`

## role_definitions
Ver `AGENTS.md`.

## tool_permissions
- builder: árbol de este repo (excepto zonas prohibidas), `judge`, `emit_delta`
- reviewer: solo lectura de artefactos + contratos
- verifier: `judge` + movimiento de estado en `queue`
- Cualquier herramienta externa (email, pagos, deploy, `rm -rf`, correr
  contra `miro-clone` fuera de lo que la tarea autoriza, etc.) requiere
  Action Gating humano.

## approval_policies
- Cambios a `contracts/`, `scripts/judge.sh`, `scripts/verify.sh` → humano obligatorio
- Acciones de alto impacto (enviar emails, pagos, borrar datos, alterar infra) → humano obligatorio
- Resto de trabajo de código/docs → puede fluir por judge + review + verifier

## acceptance_criteria
- `./scripts/judge.sh current` → PASS
- Existe una review no vacía en `state/reviews/<task>_review.md`
- No se modificaron archivos prohibidos sin aprobación registrada
- State delta emitido y legible
- `next_action` claro
- Cuando la tarea toca el harness de `eval/`: `python -m eval.metrics --selftest`
  y/o `python -m eval.score --validate-corpus` en verde, según lo que la
  tarea declare como su propio criterio (no es un check genérico de `judge.sh`,
  es específico por tarea — ver invariante 6 sobre trabajo que cruza a
  `miro-clone`).

## limitaciones conocidas
- **`PROTECTED_PATHS` está hardcodeada** en `scripts/lib_protected.sh` — igual
  que en `paios-bootstrap`, documentado ahí en `contracts/project.md` y
  `decisions/0007`/`0009`. Mitigación: `authority.can_modify` por task packet.
- **El toolkit asume bash.** Desde PowerShell: `bash scripts/<nombre>.sh`.
- **`authority.can_modify` no cruza repos** — ver invariante 6. Esta es la
  limitación nueva específica de esta integración, sin resolver todavía
  (`paios-bootstrap/decisions/0018-integracion-locagent-ts-stage2.md`, ítem 3).

## escalation_rules
- Judge falla 3 veces seguidas → blocked + notificación humana
- Intento de modificar zona prohibida → rechazo inmediato + log

## definition_of_done
Una tarea está Done solo cuando el verifier mueve el archivo a
`state/queue/done/` después de comprobar judge + review + integridad de
contratos.

## action_gating
Ver `contracts/action-gating.md`. Toda acción de alto impacto requiere
aprobación humana explícita.

## proyecciones
`projections/status.md` (humano) y `projections/compact-status.yaml`
(machine-readable) se regeneran con `scripts/update_status.sh`; ninguna es
canónica.
