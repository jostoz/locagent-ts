#!/usr/bin/env bash
set -euo pipefail

ROLE="${1:-}"
TASK_ID="${2:-}"

if [[ -z "$ROLE" || -z "$TASK_ID" ]]; then
  echo "Usage: $0 ROLE TASK-ID"
  echo "Example: $0 builder TASK-009-narracion-liviana"
  echo "Roles: builder | reviewer | verifier | project-manager | chief-of-staff"
  exit 2
fi

# P: procedimiento fijo del rol -- sección "## ROLE: <rol>" de AGENTS.md
# tal cual, sin resumir ni parafrasear, hasta el próximo "## " o EOF.
P=$(awk -v role="## ROLE: ${ROLE}" '
  $0 == role { found=1; print; next }
  found && /^## / { exit }
  found { print }
' AGENTS.md)

if [[ -z "$P" ]]; then
  echo "ERROR: no se encontró '## ROLE: ${ROLE}' en AGENTS.md" >&2
  exit 1
fi

# Σt: estado actual leído de disco -- no memoria conversacional. Packet +
# ubicación real en la cola + último delta relevante de la tarea.
TASK_FILE=""
for f in "tasks/${TASK_ID}.yaml" "state/queue/pending/${TASK_ID}" "state/queue/in_progress/${TASK_ID}" "state/queue/done/${TASK_ID}" "state/queue/blocked/${TASK_ID}"; do
  if [[ -f "$f" ]]; then
    TASK_FILE="$f"
    break
  fi
done

QUEUE_STATE="no encontrada en ninguna cola"
for stage in pending in_progress done blocked; do
  if [[ -f "state/queue/${stage}/${TASK_ID}" ]]; then
    QUEUE_STATE="$stage"
    break
  fi
done

LAST_DELTA="(ninguno todavia)"
if compgen -G "state/deltas/*_${TASK_ID}_*.yaml" > /dev/null 2>&1; then
  LAST_DELTA_FILE=$(ls -1t state/deltas/*_"${TASK_ID}"_*.yaml | head -n 1)
  LAST_DELTA=$(cat "$LAST_DELTA_FILE")
fi

# Ot: observación inmediata del entorno real -- salida actual de judge.sh
# en este checkout. No se interpreta acá; el rol decide qué hacer con ella.
JUDGE_OUTPUT=$(bash scripts/judge.sh current 2>&1 || true)

TASK_PACKET_BLOCK="(sin packet formal en tasks/ ni en ninguna cola)"
if [[ -n "$TASK_FILE" ]]; then
  TASK_PACKET_BLOCK=$(cat "$TASK_FILE")
fi

cat <<CONTEXT
=== P: procedimiento del rol (AGENTS.md, ${ROLE}) ===
${P}

=== St: estado de la tarea (leido de disco, no de conversacion previa) ===
Task: ${TASK_ID}
Cola actual: ${QUEUE_STATE}

--- Task packet (${TASK_FILE:-no encontrado}) ---
${TASK_PACKET_BLOCK}

--- Ultimo delta relevante ---
${LAST_DELTA}

=== Ot: observacion inmediata ===
--- scripts/judge.sh current ---
${JUDGE_OUTPUT}
CONTEXT
