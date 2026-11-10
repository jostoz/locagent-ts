# Granularidad de packets PAIOS (TASK-001, resuelto 2026-11-10)

> Nota de scope: `authority.can_modify` de `TASK-001-plan-step-to-paios-packet`
> es `eval/**` solamente, no `docs/**`. El ADR-0018
> (`paios-bootstrap/triggerfish/decisions/0018-integracion-locagent-ts-stage2.md`)
> pide rastrear esta decisión en `docs/TYPESCRIPT_PORT_PLAN.md` — ese archivo
> queda sin tocar por este builder (fuera de su scope autorizado); este
> documento es el registro real hasta que alguien con scope sobre `docs/**`
> lo copie o enlace allí.

## Decisión

`eval/paios_packet.py` emite **un packet por plan (tarea del corpus)**, no
uno por step. `plan_steps` queda como contexto informativo dentro del
packet; el `acceptance:` del packet está gateado en `final_acceptance` del
plan completo, nunca en el `acceptance_check` de un step individual.

## Por qué

El código original (antes de esta tarea) emitía un packet por step —
contradecía sin que nadie lo hubiera alineado una decisión ya tomada el
2026-09-17 en `docs/TYPESCRIPT_PORT_PLAN.md`, sección "Stage 2": *"un packet
por tarea con steps internos — un packet por step permitiría aceptar piezas
sin verificar el wiring final"*.

La evidencia de por qué importa: `eval/results/ablation_stress/summary.json`
documenta una corrida real (2026-09-11, condición `9b-solo-constrained`)
donde el ejecutor agregó un campo de modelo + UI + intentó el wiring de
"agregar un control de opacidad" (tarea multi-archivo, no del corpus
formal) y terminó con `tsc_exit: 2` — dos errores reales (`FILL_OPAACITY_STEP`
no definida, tipo con props faltantes) a pesar de que el *primer* paso
(agregar el campo al modelo) había compilado y pasado su propio chequeo en
aislamiento. Si el packet hubiera aceptado ese primer paso como "done" por su
cuenta (un packet por step), el wiring roto habría quedado enterrado en vez
de bloquear la tarea.

## Qué puede verificar PAIOS vs qué requiere revisión humana (invariante 6)

`verify.sh` corre contra *este* repo (`locagent-ts`); el entregable real de
un packet de plan edita `miro-clone`, un repo externo que `authority.can_modify`
no puede ver (invariante 6, `contracts/project.md`). El packet generado
(`eval/paios_packet.py::packet_for_plan`) por eso:
- Declara `external_effect.verification: human_review_required` — explícito,
  no implícito.
- Lista `external_effect.target_files` (los `target_file` de cada step) para
  que el reviewer sepa qué mirar en el diff externo.
- El texto de `acceptance:` le dice textualmente al reviewer qué debe
  inspeccionar a mano: el diff de `miro-clone` completo, y confirmar que
  *cada* `target_file` del plan fue tocado/wireado — no solo que el comando
  `final_acceptance` corrió verde (ese comando es necesario pero el reviewer
  sigue siendo quien confirma que no hay wiring fantasma que el comando no
  cubra).
- `verify.sh` en sí solo verifica evidencia local: que exista un transcript o
  referencia de score bajo `eval/results/` antes de la review, que `judge.sh
  current` pase, y que la review no esté vacía — nunca el diff de
  `miro-clone` mismo.

## Prueba de regresión ejecutada

Sobre `e2` del corpus real (`eval/miro_clone_localization.jsonl`, "Add an
optional `opacity?: number` field...", plan de 2 steps: localizar + editar;
plan cacheado en `eval/plans/e2.json`), **no** sobre el experimento de estrés
multi-archivo original de `eval/results/ablation_stress/` — ese prompt
completo no quedó preservado como script reproducible en ningún repo, solo
sus transcripts/resultados ya corridos. `e2` es la pieza real del corpus
donde la pregunta de granularidad sigue siendo observable.

Resultado: `eval/results/20261110T170457Z/planned-e2.summary.json`,
`acceptance_ok: true` — ambos steps corrieron, `tsc` y el `grep` del campo
`opacity` pasaron sobre el worktree completo, no sobre cada step aislado.

Regresión de código cubierta en
`eval/test_paios_packet.py::test_acceptance_gates_on_final_acceptance_not_step_checks`.
