# Action Gating — PAIOS

## Principio
Deny by default para acciones de alto impacto.
El builder puede proponer; solo un humano autoriza.

## Acciones que REQUIEREN aprobación humana explícita

| Acción | Motivo |
|--------|--------|
| Modificar `contracts/**` | Cambia las reglas del sistema |
| Modificar `decisions/**` (si existiera) | Cambia la verdad institucional |
| Modificar `scripts/judge.sh` o `scripts/verify.sh` | Cambia la autoridad de verificación |
| Modificar `AGENTS.md` | Cambia los contratos de roles |
| Enviar emails / mensajes externos | Efecto irreversible fuera del repo |
| Realizar pagos o transferencias | Impacto financiero |
| Borrar datos o `rm -rf` de zonas sensibles | Destrucción de estado |
| Deploy a producción / cambiar infra | Efecto externo de alto impacto |
| Publicar o mover secretos al repo | Violación de invariante de seguridad |
| Crear o rotar credenciales con privilegios amplios | Least privilege |
| Editar código fuera del working tree de este repo (p.ej. `miro-clone`) sin `authority.can_modify` explícito en la tarea | Autoridad de tarea no declarada |

## Acciones permitidas al builder sin gate humano
- Escribir dentro del árbol del repo (ver `contracts/project.md#source_hierarchy`), fuera de las zonas protegidas de arriba
- Emitir state deltas
- Reclamar tareas (`claim`)
- Correr `./scripts/judge.sh current`
- Crear archivos de review (rol reviewer)
- Actualizar proyecciones derivadas (`projections/**`)

## Protocolo cuando se requiere gate
1. El agent deja un state delta con:
   ```yaml
   next_action: await_human_approval
   unresolved:
     - action: <descripción>
       reason: action-gating
   ```
2. Mueve o marca la tarea en `state/queue/blocked/` si no puede continuar.
3. No ejecuta la acción.
4. El humano corre `./scripts/approve_gate.sh TASK-ID "motivo"` — esto es lo
   único que desbloquea un cambio a zonas protegidas. **Este script solo lo
   debe ejecutar un humano**, nunca un agent en nombre de otro rol.

## Enforcement técnico y límite de identidad
`scripts/claim.sh` toma un hash (`state/baselines/<task>.sha256`) de todas las
zonas protegidas al reclamar una tarea. `scripts/verify.sh` recalcula ese hash
al verificar: si cambió y no hay un delta `agent: human, next_action: approved`
para esa tarea, **rechaza la transición a `done`**. Eso hace ejecutable el
requisito de un registro de aprobación y evita aceptaciones accidentales.

El archivo local no autentica por sí solo a la persona que ejecutó
`approve_gate.sh`: un proceso con permisos de escritura en el checkout podría
fingir ese delta. Para acciones realmente irreversibles, el gate operativo es
además la confirmación humana fuera del agente (por ejemplo, revisar y ejecutar
el comando en una terminal propia). No se debe presentar este mecanismo como
una prueba criptográfica de identidad.

## Relación con roles
- `builder`: nunca auto-autoriza acciones de la tabla de gate.
- `verifier`: no sustituye al humano en acciones de la tabla; solo acepta
  transiciones de estado de trabajo ya autorizado por contrato.
- `project-manager` / `chief-of-staff`: pueden escalar, no saltarse el gate.
