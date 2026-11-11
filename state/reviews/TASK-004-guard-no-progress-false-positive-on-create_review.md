# Review: TASK-004-guard-no-progress-false-positive-on-create

## Verificación

1. **Test de regresión** (`eval/test_guard.py`, nuevo, gitignored por el patrón
   preexistente `test_*.py`): 4/4 OK con worktree git real (no mock), cubriendo:
   - create exitoso → NO flaggeado (el bug)
   - create sin archivo → SÍ flaggeado (claim falso sigue capturado)
   - edit con 0 cambios en tracked → SÍ flaggeado (**caso que no debe romperse, pasa**)
   - edit con cambios reales → NO flaggeado

2. **Corrida real e3** (`eval/results/20261111T133217Z/planned-e3.summary.json`):
   los 3 steps corrieron (antes se cortaba en el step 1). Step 1 (create) sin
   guard_flags; step 2 (inspect, solo lectura) con `no_progress` — comportamiento
   correcto para un step de inspección que no edita nada; step 3 (edit real,
   9 líneas en useShapes.ts) sin guard_flags. `acceptance_ok: false` por un fallo
   REAL del modelo (nunca agregó `locked?: boolean` a la interfaz BoardShape),
   capturado por el gate de final_acceptance — no por el guard.

3. **judge**: `bash scripts/judge.sh current` → `PASS: current`.

## Hallazgos

Ninguno que bloquee. Dos observaciones menores, ambas fuera del scope de esta tarea:

- El fix secundario en `eval/run_matrix.py` (pasar `target_file` real por step
  en vez de `(plan or {}).get('target_file')`, siempre None) corrige un bug
  preexistente distinto (`off_target_edit` nunca disparaba en planned mode).
  Está documentado en el diff y es consistente con el objetivo; no lo trato como
  scope creep porque era necesario para que `created_target` funcione.
- El step 2 (inspect) sigue flaggeando `no_progress`; si algún plan futuro tiene
  steps de solo lectura marcados `operation: edit`, se cortaría igual. No es
  regresión de este cambio ni lo exige la acceptance; dejarlo para una tarea
  futura si aparece en el corpus.

## Veredicto

PASS
