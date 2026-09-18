#!/usr/bin/env bash
set -euo pipefail

TASK_ID="${1:-}"
if [[ -z "$TASK_ID" ]]; then
  echo "Usage: $0 TASK-ID"
  exit 2
fi

mkdir -p state/queue/in_progress state/queue/done state/queue/blocked state/verification state/reviews state/deltas state/baselines state/attempts

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib_protected.sh
source "$ROOT/scripts/lib_protected.sh"

echo "=== Verifier for ${TASK_ID} ==="

# 1. Judge debe pasar. Cada fallo queda como delta durable, no solo el que
# escala (ver decisions/0008-persist-judge-attempt-deltas.md) — antes los
# intentos 1-2 solo se imprimían a stdout y se perdían. Si falla 3 veces
# consecutivas para esta tarea, se escala automáticamente a blocked/ (ver
# contracts/rulebook.md#5-escalation-conditions) en vez de quedar
# rechazándose para siempre en silencio.
ATTEMPTS_FILE="state/attempts/${TASK_ID}.count"
if ! JUDGE_OUTPUT=$(./scripts/judge.sh current 2>&1); then
  PREV=0
  [[ -f "$ATTEMPTS_FILE" ]] && PREV=$(cat "$ATTEMPTS_FILE")
  COUNT=$((PREV + 1))
  echo "$COUNT" > "$ATTEMPTS_FILE"
  echo "REJECT: judge failed (intento ${COUNT}/3 consecutivo para ${TASK_ID})"
  echo "$JUDGE_OUTPUT" | sed 's/^/  /'

  NEXT_ACTION="retry"
  OUTPUTS_BLOCK="outputs: []"
  if [[ $COUNT -ge 3 ]]; then
    NEXT_ACTION="escalate"
    if [[ -f "state/queue/in_progress/${TASK_ID}" ]]; then
      mv "state/queue/in_progress/${TASK_ID}" "state/queue/blocked/${TASK_ID}"
      OUTPUTS_BLOCK="outputs:
  - state/queue/blocked/${TASK_ID}"
    fi
    echo "ESCALATE: 3 fallos consecutivos de judge — moviendo ${TASK_ID} a state/queue/blocked/"
  fi

  ATS_FILE=$(date -u +"%Y%m%dT%H%M%SZ")
  ATS_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  # Sufijo con el número de intento: si dos verify.sh caen en el mismo
  # segundo (reintentos rápidos), el timestamp solo no alcanza para
  # desambiguar el nombre de archivo y un intento pisaría al anterior.
  DELTA_FILE="state/deltas/${ATS_FILE}_${TASK_ID}_verifier_attempt${COUNT}.yaml"
  {
    echo "task: ${TASK_ID}"
    echo "agent: verifier"
    echo "timestamp: ${ATS_ISO}"
    echo "inputs: []"
    echo "$OUTPUTS_BLOCK"
    echo "verification:"
    echo "  judge: FAIL"
    echo "  review: n/a"
    echo "unresolved:"
    echo "  - action: judge failed, attempt ${COUNT}/3"
    echo "    judge_output: |"
    echo "$JUDGE_OUTPUT" | sed 's/^/      /'
    echo "next_action: ${NEXT_ACTION}"
    echo "message: |"
    if [[ "$NEXT_ACTION" == "escalate" ]]; then
      echo "  3 fallos consecutivos de judge.sh para ${TASK_ID}. Escalado a blocked/."
      echo "  Un humano debe investigar y correr: ./scripts/unblock.sh ${TASK_ID} \"motivo\""
    else
      echo "  Intento ${COUNT}/3 de judge fallido para ${TASK_ID}. Ver unresolved.judge_output."
    fi
  } > "$DELTA_FILE"
  echo "Delta written: $DELTA_FILE"

  exit 1
fi
rm -f "$ATTEMPTS_FILE"
echo "[ok] judge PASS"

# 2. Integridad de zonas protegidas (contracts/, decisions/, AGENTS.md,
#    scripts/judge.sh, scripts/verify.sh). Ver contracts/action-gating.md.
BASELINE_FILE="state/baselines/${TASK_ID}.sha256"
if [[ ! -f "$BASELINE_FILE" ]]; then
  echo "REJECT: no protected-paths baseline for ${TASK_ID}. ¿Se reclamó con ./scripts/claim.sh?"
  exit 1
fi
BASELINE_HASH=$(head -n 1 "$BASELINE_FILE")
CURRENT_HASH=$(hash_protected)
if [[ "$CURRENT_HASH" != "$BASELINE_HASH" ]]; then
  if has_human_approval "$TASK_ID"; then
    echo "[ok] zonas protegidas cambiaron, pero hay aprobación humana registrada"
  else
    echo "REJECT: zonas protegidas (contracts/, decisions/, AGENTS.md, scripts/judge.sh, scripts/verify.sh)"
    echo "        fueron modificadas durante esta tarea sin aprobación humana explícita."
    echo "        Un humano debe correr: ./scripts/approve_gate.sh ${TASK_ID} \"motivo\""
    exit 1
  fi
else
  echo "[ok] zonas protegidas sin cambios desde el claim"
fi

# 2.5. Integridad del bloque authority.can_modify del propio task packet
#      (H6, TASK-008). Sin esto, un builder podía editar tasks/<id>.yaml y
#      ensancharse su propio scope sin que nada lo detectara — mismo
#      criterio que el paso 2, aplicado al task packet en vez de a
#      contracts/decisions/AGENTS.md. Tareas reclamadas antes de este fix
#      no tienen el baseline (state/baselines/<id>.authority.sha256) y se
#      degradan al comportamiento anterior en vez de romper retroactivamente.
AUTH_BASELINE_FILE="state/baselines/${TASK_ID}.authority.sha256"
if [[ -f "$AUTH_BASELINE_FILE" ]]; then
  AUTH_BASELINE_HASH=$(cat "$AUTH_BASELINE_FILE")
  AUTH_CURRENT_HASH=$(hash_authority_block "tasks/${TASK_ID}.yaml")
  if [[ "$AUTH_CURRENT_HASH" != "$AUTH_BASELINE_HASH" ]]; then
    if has_human_approval "$TASK_ID"; then
      echo "[ok] authority.can_modify cambió, pero hay aprobación humana registrada"
    else
      echo "REJECT: authority.can_modify de tasks/${TASK_ID}.yaml cambió desde el claim"
      echo "        sin aprobación humana explícita. Un humano debe correr:"
      echo "        ./scripts/approve_gate.sh ${TASK_ID} \"motivo\""
      exit 1
    fi
  else
    echo "[ok] authority.can_modify sin cambios desde el claim"
  fi
fi

# 3. Authority scope opcional por tarea (whitelist adicional, ADR-0006).
#    Si tasks/<id>.yaml no declara authority.can_modify, no hay restricción
#    extra — el blacklist del paso 2 sigue aplicando siempre, con o sin esto.
TASK_FILE="tasks/${TASK_ID}.yaml"
OUT_OF_SCOPE="$(check_task_authority "$TASK_ID" "$TASK_FILE")"
if [[ -n "$OUT_OF_SCOPE" ]]; then
  echo "REJECT: archivos fuera de authority.can_modify para ${TASK_ID}:"
  echo "$OUT_OF_SCOPE" | sed 's/^/  - /'
  exit 1
fi
echo "[ok] cambios dentro de authority.can_modify (o sin authority declarado)"

# 4. Debe existir al menos un delta de la tarea
# (emit_delta.sh nombra los archivos ${TIMESTAMP_SAFE}_${TASK_ID}_${AGENT}.yaml)
DELTA_MATCH=""
if compgen -G "state/deltas/*_${TASK_ID}_*.yaml" > /dev/null 2>&1; then
  DELTA_MATCH=$(ls -1t state/deltas/*_"${TASK_ID}"_*.yaml | head -n 1)
  echo "[ok] state delta exists: ${DELTA_MATCH}"
else
  echo "REJECT: no state delta found for ${TASK_ID} (expected state/deltas/*_${TASK_ID}_*.yaml)"
  exit 1
fi

# 5. Review honesta (un solo reviewer — ver decisions/0003-single-honest-review.md)
REVIEW="state/reviews/${TASK_ID}_review.md"

if [[ ! -f "$REVIEW" ]]; then
  echo "REJECT: missing $REVIEW"
  exit 1
fi
if [[ ! -s "$REVIEW" ]]; then
  echo "REJECT: $REVIEW is empty"
  exit 1
fi
if ! grep -qE "RULE:|ISSUE:|PASS|APPROVE" "$REVIEW"; then
  echo "WARN: $REVIEW no contiene marcadores típicos (RULE:/ISSUE:/PASS/APPROVE). Se acepta por no estar vacía, pero se recomienda seguir el formato del rulebook."
fi
echo "[ok] review present and non-empty"

# 6. Aceptar transición de estado
if [[ -f "state/queue/in_progress/${TASK_ID}" ]]; then
  mv "state/queue/in_progress/${TASK_ID}" "state/queue/done/${TASK_ID}"
elif [[ -f "tasks/${TASK_ID}.yaml" ]]; then
  cp "tasks/${TASK_ID}.yaml" "state/queue/done/${TASK_ID}"
else
  touch "state/queue/done/${TASK_ID}"
fi

# 7. Registrar verification (timestamp filesystem-safe en el nombre, ISO en el contenido)
TS_FILE=$(date -u +"%Y%m%dT%H%M%SZ")
TS_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
VER_FILE="state/verification/${TASK_ID}_${TS_FILE}.yaml"

cat > "$VER_FILE" <<YAML
task: ${TASK_ID}
timestamp: ${TS_ISO}
result: ACCEPTED
judge: PASS
protected_paths: unchanged_or_approved
review: present
delta_ref: ${DELTA_MATCH}
YAML

# 8. Emitir delta de aceptación del verifier (evidencia durable, no solo verification/)
cat > "state/deltas/${TS_FILE}_${TASK_ID}_verifier.yaml" <<YAML
task: ${TASK_ID}
agent: verifier
timestamp: ${TS_ISO}
inputs:
  - ${DELTA_MATCH}
  - ${REVIEW}
outputs:
  - state/queue/done/${TASK_ID}
  - ${VER_FILE}
verification:
  judge: PASS
  review: PASS
unresolved: []
next_action: none
message: |
  Task accepted by verifier. Protected paths unchanged or human-approved. Moved to done.
YAML

echo "ACCEPTED: ${TASK_ID} → state/queue/done/"
echo "Verification record: ${VER_FILE}"
