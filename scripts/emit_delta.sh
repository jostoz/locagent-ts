#!/usr/bin/env bash
set -euo pipefail

TASK="${1:-}"
AGENT="${2:-}"
MSG="${3:-}"
# Opcional (ADR-0015, narración liviana): array JSON de {path, change} con
# el detalle estructurado de qué cambió. Sin este 4º argumento, outputs
# queda [] como en el comportamiento anterior — firma vieja sigue válida.
OUTPUTS_JSON="${4:-}"

if [[ -z "$TASK" || -z "$AGENT" || -z "$MSG" ]]; then
  echo "Usage: $0 TASK-ID AGENT \"message\" ['[{\"path\":\"...\",\"change\":\"...\"}]']"
  echo "Example: $0 TASK-001 builder \"ver outputs\" '[{\"path\":\"scripts/judge.sh\",\"change\":\"added current check\"}]'"
  exit 2
fi

# message es un puntero (1-2 líneas), no una narrativa -- ver
# decisions/0015-narracion-liviana.md. WARN, no FAIL: es convención, no
# gate -- mismo criterio que el resto del rulebook para chequeos suaves.
MSG_LINES=$(printf '%s' "$MSG" | grep -c '$' || true)
if [[ ${#MSG} -gt 200 || $MSG_LINES -gt 1 ]]; then
  echo "WARN: message parece narrativo (>200 chars o multilínea)." >&2
  echo "      Convención de narración liviana (ADR-0015): message es un puntero" >&2
  echo "      corto, el detalle real va en outputs estructurado (4º argumento)." >&2
fi

mkdir -p state/deltas
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
TS_SAFE=$(date -u +"%Y%m%dT%H%M%SZ")

# Sanitizar nombre de archivo (sin ":" — inseguro en NTFS/Windows)
SAFE_AGENT=$(echo "$AGENT" | tr -cd 'a-zA-Z0-9.-')
FILE="state/deltas/${TS_SAFE}_${TASK}_${SAFE_AGENT}.yaml"

OUTPUTS_BLOCK="outputs: []"
if [[ -n "$OUTPUTS_JSON" ]]; then
  if command -v jq >/dev/null 2>&1; then
    if ! echo "$OUTPUTS_JSON" | jq -e 'type == "array" and length > 0 and all(.[]; has("path") and has("change"))' >/dev/null 2>&1; then
      echo "ERROR: outputs debe ser un array JSON no vacío de objetos {path, change}" >&2
      exit 2
    fi
    # tojson produce un scalar YAML válido (string JSON entre comillas) --
    # evita reinventar el escapado de YAML a mano.
    OUTPUTS_BLOCK="outputs:
$(echo "$OUTPUTS_JSON" | jq -r '.[] | "  - path: \(.path | tojson)\n    change: \(.change | tojson)"')"
  else
    echo "WARN: jq no disponible, outputs no se pudo estructurar/validar -- guardado como raw" >&2
    OUTPUTS_BLOCK="outputs:
  - raw: |
$(echo "$OUTPUTS_JSON" | sed 's/^/      /')"
  fi
fi

cat > "$FILE" <<YAML
task: ${TASK}
agent: ${AGENT}
timestamp: ${TS}
inputs: []
${OUTPUTS_BLOCK}
verification:
  judge: pending
  review: pending
unresolved: []
next_action: review
message: |
  ${MSG}
YAML

echo "Delta written: $FILE"
