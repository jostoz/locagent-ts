#!/usr/bin/env bash
set -euo pipefail

# Solo un humano debe ejecutar este script (ver contracts/rulebook.md#5-escalation-conditions).
# Desbloquea una tarea escalada a state/queue/blocked/ (p. ej. por 3 fallos
# consecutivos de judge.sh) devolviéndola a pending/ y reiniciando su
# contador de intentos, para que el ciclo claim -> ... -> verify pueda
# volver a correr desde cero.

TASK_ID="${1:-}"
REASON="${2:-}"

if [[ -z "$TASK_ID" || -z "$REASON" ]]; then
  echo "Usage: $0 TASK-ID \"reason\""
  echo "Example: $0 TASK-003 \"fixed missing contracts/action-gating.md, safe to retry\""
  exit 2
fi

if [[ ! -f "state/queue/blocked/${TASK_ID}" ]]; then
  echo "ERROR: ${TASK_ID} no está en state/queue/blocked/"
  exit 1
fi

mkdir -p state/queue/pending state/deltas state/attempts
mv "state/queue/blocked/${TASK_ID}" "state/queue/pending/${TASK_ID}"
rm -f "state/attempts/${TASK_ID}.count"

TS_FILE=$(date -u +"%Y%m%dT%H%M%SZ")
TS_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
FILE="state/deltas/${TS_FILE}_${TASK_ID}_human.yaml"

cat > "$FILE" <<YAML
task: ${TASK_ID}
agent: human
timestamp: ${TS_ISO}
inputs: []
outputs:
  - state/queue/pending/${TASK_ID}
verification:
  judge: n/a
  review: n/a
unresolved: []
next_action: unblocked
message: |
  ${REASON}
YAML

echo "Unblocked: ${TASK_ID} → state/queue/pending/. Contador de intentos reiniciado."
echo "Human delta: $FILE"
