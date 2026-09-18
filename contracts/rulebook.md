# Rulebook — PAIOS

## 1. Output format

### State Delta (obligatorio)
Todo trabajo de un agent debe producir un archivo en `state/deltas/` con este esquema mínimo:

```yaml
task: TASK-XXX
agent: builder|reviewer|verifier|project-manager|human
timestamp: YYYY-MM-DDTHH:MM:SSZ
inputs: []
outputs: []
verification:
  judge: PASS|FAIL|pending|n/a
  review: PASS|FAIL|pending|n/a
unresolved: []
next_action: string
message: string
```

### Narración liviana (ver decisions/0015-narracion-liviana.md)
`message` es un puntero corto (1-2 líneas), no una narrativa — el detalle
real de qué cambió y por qué va en `outputs`, estructurado:
```yaml
outputs:
  - path: src/algo.ts
    change: descripción concreta y corta de qué cambió ahí
```
`scripts/emit_delta.sh` acepta un 4º argumento opcional (JSON array de
`{path, change}`) para construir este bloque; sin ese argumento, `outputs`
queda `[]` como antes (compatible con la firma anterior). El motivo:
ADR-0010 midió que la ceremonia narrativa en prosa —no el mecanismo— era
el costo real, y ninguno de los bugs reales documentados ahí lo atrapó un
párrafo narrado; lo atraparon tests o verificación en entorno real.

### Reviews
Un solo review honesto por tarea (ver decisions/0003-single-honest-review.md),
en `state/reviews/<task_id>_review.md`, usando la plantilla
`contracts/templates/review-template.md`: checklist de
`contracts/acceptance.md` (PASS/FAIL por criterio) + hallazgos, sin
preámbulo narrativo. Formato de findings:
```
RULE: contracts/rulebook.md#seccion
ISSUE: descripción concreta y accionable
```

## 2. Conventions
- Nombres de tareas: `TASK-XXX` o `TASK-XXX-descripcion-corta.yaml`
- **El nombre de archivo del task packet es el ID canónico**, sin la extensión.
  Todos los scripts (`claim.sh`, `verify.sh`, `queue.sh`) reciben y buscan por
  ese nombre: para `tasks/TASK-007-hallazgos-portabilidad.yaml` el ID es
  `TASK-007-hallazgos-portabilidad`. El campo `id:` de adentro del YAML es
  **informativo**, no lo lee ningún script — puede ser la forma corta
  (`TASK-007`) sin que nada se rompa. Hallazgo H4 de TASK-007.
- Invocación de scripts: en Windows, desde PowerShell usar
  `bash scripts/<nombre>.sh`; desde Git Bash, `./scripts/<nombre>.sh` funciona
  directo. Ver contracts/project.md#limitaciones-conocidas.
- Timestamps siempre en UTC (ISO 8601)
- Un delta por acción coherente (no acumular horas de trabajo en un solo delta opaco)
- Preferir archivos pequeños y legibles sobre logs monolíticos

## 3. Edge cases
- Si el judge no puede ejecutarse → estado `blocked`, no inventar PASS
- Si falta una review → el verifier no puede aceptar
- Si hay conflicto entre review_a y review_b → escalar, no promediar en silencio
- Si una tarea queda a medias por caída de sesión → el siguiente agent reanuda desde el último delta + estado de queue

## 4. Forbidden actions
- Auto-aceptar (mover a `done`) siendo builder
- Modificar contracts/, decisions/, scripts/judge.sh, scripts/verify.sh sin aprobación humana
- Parchear manualmente una salida solo para hacer pasar el judge
- Introducir secretos (API keys, tokens, passwords) en cualquier archivo del repo
- Ejecutar acciones de alto impacto sin Action Gating
- Ignorar el rulebook y "arreglar después"

## 5. Escalation conditions
- 3 fallos consecutivos de judge
- Desacuerdo persistente entre reviewers
- Intento de escritura en zona prohibida
- Ambigüedad de requisitos que bloquee progreso
- Coste o loops anómalos detectados

### Enforcement técnico (no solo documentación)
`scripts/verify.sh` cuenta los fallos consecutivos de judge por tarea en
`state/attempts/<task>.count`. Al llegar a 3, mueve la tarea a
`state/queue/blocked/` automáticamente y emite un delta
`agent: verifier, next_action: escalate`. Solo un humano puede recuperarla,
corriendo `./scripts/unblock.sh TASK-ID "motivo"` (nunca un agent en nombre
de otro rol) — esto reinicia el contador y devuelve la tarea a `pending/`.

## 6. TODO(agent) protocol
Cuando un agent no puede resolver algo dentro de su rol:

```
TODO(agent): descripción clara de lo que falta
BLOCKED_BY: <humano|otro-rol|recurso>
NEXT: acción recomendada
```

Dejar el TODO dentro del state delta y, si aplica, mover la tarea a `state/queue/blocked/`.

## 7. Principio operativo
Nunca parchear manualmente una salida para corregir algo que debería haberse expresado como regla.
