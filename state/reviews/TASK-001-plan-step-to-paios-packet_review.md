# Review — TASK-001-plan-step-to-paios-packet

Reviewer: reviewer (single honest review, per contracts/project.md invariante 4)
Fecha: 2026-11-10

## Artefactos revisados
- `eval/paios_packet.py` (diff en `eval/_task001_diff_for_review.txt`)
- `eval/test_paios_packet.py` (3 tests, ver abajo)
- `docs/TYPESCRIPT_PORT_PLAN.md` sección Stage 2 ("Resuelto (TASK-001, 2026-11-10)")
- `tasks/TASK-e2.yaml` (packet real generado y reclamado en `state/queue/in_progress/`)
- `eval/results/20261110T170457Z/planned-e2.summary.json` (corrida end-to-end real)

## Verificación de criterios de aceptación

### a) Módulo que traduce plan → packets: OK
`eval/paios_packet.py::packet_for_plan(plan)` produce un YAML válido para `tasks/`.
Verificado con corrida real: `tasks/TASK-e2.yaml` existe, es legible por el
subconjunto de línea de YAML que leen `claim.sh`/`verify.sh`, y fue reclamado
(`state/queue/in_progress/TASK-e2`).

### b) Granularidad decidida y justificada por escrito: OK
`docs/TYPESCRIPT_PORT_PLAN.md:590` tiene la sección "**Resuelto (TASK-001, 2026-11-10)**"
con la justificación completa: un packet por tarea del corpus (plan completo), no por paso;
explica el failure mode de wiring parcial que motivó la decisión original del 2026-09-17 y
por qué el código anterior (un packet por step) la contradecía sin haberse alineado. No es
código sin explicación: la sección cita explícitamente `forge-platform/triad-integration/evals/opacity/proposal.md`
y el test de regresión que cubre el failure mode.

### c) Prueba de regresión real + documentación de qué puede verificar PAIOS: OK
- `eval/results/20261110T170457Z/planned-e2.summary.json`: `"acceptance_ok": true`,
  ambos steps del plan corrieron (step 1 inspect, step 2 edit con diff real en
  `src/board/useShapes.ts`), y las dos acceptance commands pasaron
  (`npx tsc --noEmit` exit 0; `grep opacity?: number` matched).
- `tasks/TASK-e2.yaml` documenta explícitamente la frontera de verificación:
  - `authority.can_modify: [eval/**, docs/**]` — solo este repo (invariante 6).
  - `external_effect.repository: "C:/Users/joz/Documents/miro-clone"` +
    `verification: human_review_required` — el diff real en miro-clone NO es
    verificable por `verify.sh`.
  - El texto de `acceptance:` le dice al reviewer exactamente qué inspeccionar a mano:
    el diff externo Y que cada `target_file` del plan fue realmente tocado, y que un
    `acceptance_check` individual de step no basta — solo `final_acceptance` sobre todo
    el worktree cuenta.

### d) `bash scripts/judge.sh current`: OK
```
NOTE: shellcheck not installed, skipping script lint
PASS: current
Wall time: 4.09 seconds
```

## Test de regresión (el que importa)
```
test_acceptance_gates_on_final_acceptance_not_step_checks ... ok
test_packet_is_one_per_plan_not_per_step ... ok
test_write_one_packet_for_whole_plan ... ok
Ran 3 tests in 0.021s — OK
```
Confirmado: el acceptance del packet NUNCA promueve el `acceptance_check` de un step
individual al nivel top; solo `final_acceptance` del plan completo aparece en `acceptance:`.

## Hallazgos
Ninguno. No hay RULE/ISSUE que reportar.

Observación menor (no bloqueante, no es ISSUE): la corrida real sobre `e2` usa un plan de
2 steps donde step 1 es "inspect" y step 2 es "edit" — el failure mode original del estrés
de opacidad era multi-archivo con wiring cruzado entre 3 archivos. El doc lo reconoce
explícitamente ("no sobre el experimento de estrés multi-archivo original... ese prompt
completo no quedó preservado como script reproducible") y justifica por qué `e2` es la pieza
real del corpus donde la pregunta sigue siendo observable. Es una limitación honestamente
documentada, no un defecto: la regresión que importa (gate en final_acceptance vs step check)
está cubierta por el test unitario, y la corrida real valida el pipeline completo claim→build→judge→review→verify sobre un caso del corpus real.

## Veredicto
PASS
