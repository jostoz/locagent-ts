# Stage 2: packets PAIOS para planes de `eval/planner.py`

La unidad de trabajo es un **packet por paso del plan**, no uno por tarea de
corpus completa. Cada paso ya tiene un único `target_file`, una operación y un
check de aceptación; aislarlo permite retry, review y bloqueo sin reabrir los
otros pasos.

`python -m eval.paios_packet eval/plans/<id>.json` crea esos packets en
`tasks/`. El packet conserva la acción prevista en `external_effect`, pero su
autoridad local solo abarca `eval/**` y `docs/**`.

Esto es intencional: el ejecutor edita `C:/Users/joz/Documents/miro-clone`, un
repositorio distinto. `verify.sh` compara el diff del checkout de locagent-ts
y no puede certificar el diff externo. Por eso una corrida real debe guardar
un transcript o score local y una persona debe revisar el diff de miro-clone;
el campo `external_effect.verification: human_review_required` hace visible
esa frontera en vez de simular una garantía que no existe.

La prueba de regresión de opacidad queda bloqueada hasta que haya aprobación
humana explícita para editar el checkout de `miro-clone`. Antes de esa acción,
las pruebas del traductor y los gates locales sí son reproducibles aquí.
