#!/usr/bin/env bash
set -euo pipefail

# El builder (o cualquier rol no-humano) corre esto cuando necesita una
# acción de la tabla de contracts/action-gating.md que no puede autorizarse
# a sí mismo. Antes de este script, el protocolo pedía que el agent
# escribiera ese delta a mano — era el único paso del gate sin script,
# sin validación de forma antes de tocar disco.
#
# Registra el delta que action-gating.md exige (next_action:
# await_human_approval, unresolved con reason: action-gating) y mueve la
# tarea a state/queue/blocked/, porque el agent no puede continuar sin la
# acción que está pidiendo autorizar.
#
# El humano corre después:
#   ./scripts/approve_gate.sh TASK-ID "motivo"   (autoriza el cambio a zonas protegidas)
#   ./scripts/unblock.sh TASK-ID "motivo"         (devuelve la tarea a pending/)

TASK_ID="${1:-}"
AGENT="${2:-}"
ACTION="${3:-}"

if [[ -z "$TASK_ID" || -z "$AGENT" || -z "$ACTION" ]]; then
  echo "Usage: $0 TASK-ID AGENT \"descripción de la acción que requiere aprobación\""
  echo "Example: $0 TASK-007 builder \"modificar contracts/rulebook.md para agregar regla de retries\""
  exit 2
fi

if [[ "$AGENT" == "human" ]]; then
  echo "ERROR: 'human' no pide aprobación, la otorga. Usa ./scripts/approve_gate.sh en su lugar."
  exit 2
fi

mkdir -p state/deltas state/queue/blocked

TS_FILE=$(date -u +"%Y%m%dT%H%M%SZ")
TS_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Sanitizar nombre de archivo (sin ":" — inseguro en NTFS/Windows), mismo
# criterio que emit_delta.sh
SAFE_AGENT=$(echo "$AGENT" | tr -cd 'a-zA-Z0-9.-')
FILE="state/deltas/${TS_FILE}_${TASK_ID}_${SAFE_AGENT}.yaml"

OUTPUTS_BLOCK="outputs: []"
if [[ -f "state/queue/in_progress/${TASK_ID}" ]]; then
  mv "state/queue/in_progress/${TASK_ID}" "state/queue/blocked/${TASK_ID}"
  OUTPUTS_BLOCK="outputs:
  - state/queue/blocked/${TASK_ID}"
  echo "Moved ${TASK_ID}: in_progress -> blocked"
fi

cat > "$FILE" <<YAML
task: ${TASK_ID}
agent: ${AGENT}
timestamp: ${TS_ISO}
inputs: []
${OUTPUTS_BLOCK}
verification:
  judge: n/a
  review: n/a
unresolved:
  - action: |
      ${ACTION}
    reason: action-gating
next_action: await_human_approval
message: |
  ${ACTION}
  Requiere aprobación humana explícita (contracts/action-gating.md). Un
  humano debe correr: ./scripts/approve_gate.sh ${TASK_ID} "motivo", y luego
  ./scripts/unblock.sh ${TASK_ID} "motivo" para devolver la tarea a pending/.
YAML

echo "Approval requested: $FILE"
echo "next_action: await_human_approval — ${TASK_ID} bloqueada hasta aprobación humana."
