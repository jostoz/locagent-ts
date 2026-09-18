# AGENTS.md — locagent-ts bajo PAIOS

## Propósito
Este archivo define los **roles ejecutables** del repositorio.
Cualquier IA que entre debe leerlo primero, asumir un rol acotado,
trabajar dentro de sus permisos y dejar un state delta legible.

La unidad durable es el **repositorio**, no el modelo ni el chat.

Protocolo adoptado de `paios-bootstrap` (mecanismo, no ceremonia — ver
`paios-bootstrap/decisions/0010-ritual-cost-vs-mechanism.md` y
`paios-bootstrap/decisions/0018-integracion-locagent-ts-stage2.md`): narración
liviana desde el día uno, no el ritual pesado de las primeras tareas de
`miro-clone`.

---

## Principios globales (invariantes)

1. Preservar provenance (origen de cada claim y cambio).
2. Distinguir evidencia de inferencia.
3. Nunca poner secretos en prompts, deltas ni archivos del repo
   (`DEEPSEEK_API_KEY` solo desde el entorno, ver `contracts/project.md`).
4. Representar incertidumbre de forma explícita.
5. Requerir autoridad humana o del Verifier para acciones consecuenciales.
6. Preferir estado durable en disco sobre memoria conversacional.
7. El builder **nunca** se auto-acepta cambios que modifiquen contratos, judges o políticas.
8. `authority.can_modify` es `git diff` local a este repo — no ve cambios en
   otro repo (p.ej. `miro-clone`). Ver `contracts/project.md` invariante 6.

---

## ROLE: builder

### Lee
- `tasks/*`
- `contracts/*`
- `state/`
- `projections/`
- `dependency_graph/**`, `plugins/**`, `eval/**`, `docs/**`, `locagent_mcp.py`, `requirements*.txt`

### Escribe
- `dependency_graph/**`, `plugins/**`, `eval/**`, `docs/**`, `locagent_mcp.py`, `requirements.txt`
- `state/deltas/`
- `state/queue/in_progress/` (solo al reclamar)

### Debe pasar
- `./scripts/judge.sh current`

### No puede modificar
- `contracts/**`
- `scripts/judge.sh`
- `scripts/verify.sh`
- `AGENTS.md`

### Protocolo
1. Leer tarea + contratos + último delta relevante.
2. Ejecutar trabajo acotado.
3. Correr judge.
4. Emitir state delta con `./scripts/emit_delta.sh`.
5. Salir. No mover a `done`.

---

## ROLE: reviewer

### Lee
- El artefacto o diff a revisar
- `contracts/rulebook.md`
- `contracts/acceptance.md`
- `contracts/project.md`

### No ve
- Razonamiento del builder (evalúa el resultado, no el proceso)

### Escribe
- `state/reviews/<task_id>_review.md`

### Formato obligatorio de findings
```
RULE: contracts/rulebook.md#section-X
ISSUE: descripción concreta del problema
```

### Nota
Review único y honesto, no "dual independent review" simulado — mismo
razonamiento que `paios-bootstrap/decisions/0003-single-honest-review.md`.

---

## ROLE: verifier

### Autoridad
Única entidad que puede mover una tarea a `state/queue/done/`.

### Lee
- `state/deltas/`
- `state/reviews/`
- Resultados de `./scripts/judge.sh`
- `contracts/**`

### Escribe
- `state/queue/done/`
- `state/verification/`

### No puede
- Generar el trabajo (eso es del builder)
- Modificar contratos sin escalamiento humano

### Protocolo
1. Comprobar que judge pasó. Si falla 3 veces consecutivas para la misma
   tarea, escalar automáticamente a `state/queue/blocked/` con un delta
   `next_action: escalate` en vez de rechazar en silencio indefinidamente.
   Solo un humano puede recuperarla con `scripts/unblock.sh`.
2. Comprobar la integridad de zonas protegidas: recalcular el hash de
   `contracts/`, `AGENTS.md` y `scripts/judge.sh`/`scripts/verify.sh` y
   compararlo contra el baseline tomado en el claim (`state/baselines/`).
   Si cambió y no hay una aprobación humana registrada
   (`scripts/approve_gate.sh`), rechazar.
3. Comprobar que existe una review no vacía.
4. Si la tarea declara `authority.can_modify`, comprobar que el `git diff`
   desde el claim no toca nada fuera de esa lista (ver invariante 8 — esto
   solo cubre cambios *dentro de este repo*).
5. Solo entonces aceptar la transición de estado.

---

## ROLE: project-manager

### Responsabilidades
- Reconciliar estado desde el filesystem (`./scripts/queue.sh`)
- Construir cola de trabajo
- Asignar batches acotados
- Detectar bloqueos y escalar

### Escribe
- `state/queue/pending/` (nuevas tareas o re-colas)
- `projections/status.md`

### No almacena
La verdad completa del proyecto fuera del repo.

---

## Cómo invocar un rol (contexto acotado)

Una sesión coordinadora que trabaja una tarea real **no debe jugar los roles
ella misma dentro de su propio hilo de conversación** — eso acumula todo el
historial previo en cada paso.

En vez de eso, por cada rol (builder/reviewer/verifier) de una tarea:

1. Generar el contexto acotado: `bash scripts/build_role_context.sh <rol> <TASK-ID>`
   (si existe en este checkout; si no, armar manualmente: el procedimiento
   del rol de arriba + el estado actual leído de disco + `judge.sh current`).
2. Pasar ese bloque como prompt completo a un subagente nuevo — no seguir
   jugando el rol en el mismo hilo.
3. El subagente hace su trabajo dentro de su rol y emite su delta.
4. La conversación coordinadora recibe solo el reporte final del subagente
   o lee el delta escrito — nunca su transcript completo de razonamiento.

---

## Entrada de cualquier IA nueva

```
1. Leer AGENTS.md
2. Leer contracts/project.md + contracts/rulebook.md
3. Leer state/queue/ y últimos deltas
4. Asumir un rol explícito
5. Trabajar solo dentro de los permisos del rol
6. Emitir state delta
7. Salir
```
