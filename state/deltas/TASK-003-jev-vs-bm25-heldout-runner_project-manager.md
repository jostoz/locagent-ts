# Delta — TASK-003-jev-vs-bm25-heldout-runner

- rol: project-manager
- fecha: 2026-09-20
- accion: revisar packet (reemplaza la version anterior, aun pending, sin
  builder claim registrado)
- packet: tasks/TASK-003-jev-vs-bm25-heldout-runner.yaml
- cola: state/queue/pending/TASK-003-jev-vs-bm25-heldout-runner (copia
  byte-equivalente del packet, verificada con `cmp`)

## Motivo de la revision

El packet anterior acotaba el entregable a un runner de solo-localizacion
(BM25 vs Jev sobre un caso held-out path-free, sin editar, sin worktree de
escritura). El usuario aprobo un experimento mas amplio que esa version
subestimaba: se requiere un runner de EDICION held-out completa, con estado
del ejecutor gestionado por el runtime GESTADO, comparando dos brazos
equivalentes (Gestado+BM25+AST vs Gestado+Jev+AST) bajo el mismo prompt,
modelo, sandbox, commit y acceptance gates, con la garantia explicita de que
el ranking/snippets crudos de Jev nunca llegan al ejecutor -- solo un
paquete canonico de entidades resuelto contra el grafo AST.

## Resumen del packet revisado

Se reescribe integramente objective/context/acceptance de TASK-003 para
exigir:
1. Dos brazos experimentales end-to-end (localizacion + edicion real), no
   solo localizacion.
2. Gestion de estado entre pasos con GESTADO
   (`C:/Users/joz/orca/workspaces/Gestado/cabezon`) en lugar del carry ad
   hoc de eval/render_step.py.
3. Equivalencia estricta entre brazos: mismo prompt path-free, mismo
   modelo, mismo `--sandbox-image`, mismo commit pineado de miro-clone,
   mismos acceptance gates del fixture held-out.
4. Handoff canonico obligatorio en el brazo Jev: los paths de jegrep se
   canonicalizan contra ids de entidad del grafo AST antes de construir el
   paquete que ve el ejecutor; el ranking/snippets crudos de jegrep quedan
   prohibidos como input del ejecutor; todo path no resoluble cuenta como
   guard rejection.
5. Ambos brazos editan sobre un worktree aislado y descartable de
   miro-clone (mismo mecanismo que eval/isolated_env.py /
   eval/run_matrix.py), ahora con sandbox de eval/container/ (a diferencia
   de la version anterior, que explicitamente lo eximia por ser
   solo-lectura).
6. El artefacto de resultado por brazo debe registrar: exito end-to-end de
   la edicion, metricas de localizacion a nivel de entidad
   (eval/metrics.py), tokens/llamadas, wall time, guard rejections, tamano
   del paquete canonico entregado, y resultado de la validacion
   post-edicion.

authority.can_modify sigue acotado a `eval/**` -- no toca `contracts/`,
`scripts/judge.sh`, `scripts/verify.sh`, `AGENTS.md`, ni TASK-001 (sin
tocar). Por contracts/project.md invariante 6, el trabajo real de edicion
ocurre sobre miro-clone (repo externo); la tarea exige explicitamente que
artefacto de este repo certifica que ambos brazos tocaron ese worktree,
aplicaron una edicion real y que valido cada uno. Prioridad high, estado
pending (sin claim de builder registrado sobre la version anterior --
ningun trabajo de builder se pierde con esta revision).

## next_action
Builder reclama TASK-003-jev-vs-bm25-heldout-runner (claim.sh) e implementa
el runner de dos brazos (Gestado+BM25+AST, Gestado+Jev+AST), el fixture
held-out de tipo edit, y la canonicalizacion Jev->AST dentro de `eval/**`;
correr `bash scripts/judge.sh current` y emitir su propio delta con
`scripts/emit_delta.sh` antes de pedir review.
