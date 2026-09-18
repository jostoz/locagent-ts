#!/usr/bin/env bash
set -euo pipefail

# Solo un humano debe ejecutar este script (ver contracts/action-gating.md).
# Registra la aprobación explícita de un cambio a zonas protegidas
# (contracts/, decisions/, AGENTS.md, scripts/judge.sh, scripts/verify.sh)
# hecho durante una tarea. verify.sh exige este delta cuando detecta que el
# hash de zonas protegidas cambió desde el claim.

TASK_ID="${1:-}"
REASON="${2:-}"

if [[ -z "$TASK_ID" || -z "$REASON" ]]; then
  echo "Usage: $0 TASK-ID \"reason\""
  echo "Example: $0 TASK-003 \"human reviewed and approved rulebook update for X\""
  exit 2
fi

mkdir -p state/deltas
TS_FILE=$(date -u +"%Y%m%dT%H%M%SZ")
TS_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
FILE="state/deltas/${TS_FILE}_${TASK_ID}_human.yaml"

cat > "$FILE" <<YAML
task: ${TASK_ID}
agent: human
timestamp: ${TS_ISO}
inputs: []
outputs: []
verification:
  judge: n/a
  review: n/a
unresolved: []
next_action: approved
message: |
  ${REASON}
YAML

echo "Human approval recorded: $FILE"
echo "verify.sh ahora aceptará cambios a zonas protegidas para ${TASK_ID}."
