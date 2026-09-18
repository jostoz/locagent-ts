#!/usr/bin/env bash
set -euo pipefail

# Regenera projections/status.md desde el estado en disco.
# Es una proyección derivada: puede quedar obsoleta; el canónico es state/.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=lib_protected.sh
source "$ROOT/scripts/lib_protected.sh"

OUT="projections/status.md"
OUT_COMPACT="projections/compact-status.yaml"
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

mkdir -p projections state/queue/pending state/queue/in_progress state/queue/blocked state/queue/done \
  state/deltas state/reviews state/verification state/baselines state/attempts

list_dir() {
  local dir="$1"
  if compgen -G "${dir}/*" > /dev/null 2>&1; then
    ls -1 "$dir" | sed 's/^/- /'
  else
    echo "- (vacío)"
  fi
}

count_dir() {
  local dir="$1"
  if compgen -G "${dir}/*" > /dev/null 2>&1; then
    ls -1 "$dir" | wc -l | tr -d ' '
  else
    echo "0"
  fi
}

{
  echo "# Status — PAIOS"
  echo ""
  echo "> Proyección derivada generada automáticamente."
  echo "> Autoridad: \`state/\` + \`contracts/\` + \`decisions/\`."
  echo ""
  echo "- Generado: ${TS}"
  echo "- Script: \`scripts/update_status.sh\`"
  echo ""
  echo "## Cola"
  echo ""
  echo "| Estado | Cantidad |"
  echo "|--------|----------|"
  echo "| pending | $(count_dir state/queue/pending) |"
  echo "| in_progress | $(count_dir state/queue/in_progress) |"
  echo "| blocked | $(count_dir state/queue/blocked) |"
  echo "| done | $(count_dir state/queue/done) |"
  echo ""
  echo "### pending"
  list_dir state/queue/pending
  echo ""
  echo "### in_progress"
  list_dir state/queue/in_progress
  echo ""
  echo "### blocked"
  list_dir state/queue/blocked
  echo ""
  echo "### done"
  list_dir state/queue/done
  echo ""
  echo "## Últimos deltas (max 10)"
  echo ""
  if compgen -G "state/deltas/*.yaml" > /dev/null 2>&1; then
    ls -1t state/deltas/*.yaml | head -n 10 | sed 's/^/- /'
  else
    echo "- (ninguno)"
  fi
  echo ""
  echo "## Reviews"
  echo ""
  if compgen -G "state/reviews/*" > /dev/null 2>&1; then
    ls -1 state/reviews/ | sed 's/^/- /'
  else
    echo "- (ninguna)"
  fi
  echo ""
  echo "## Verifications recientes"
  echo ""
  if compgen -G "state/verification/*" > /dev/null 2>&1; then
    ls -1t state/verification/ | head -n 5 | sed 's/^/- /'
  else
    echo "- (ninguna)"
  fi
  echo ""
  echo "## Next actions sugeridas"
  echo ""
  echo "1. Ejecutar \`./scripts/queue.sh\` para reconciliar."
  echo "2. Si hay pending: claim + trabajo + delta + dual review + verify."
  echo "3. Si hay blocked: revisar unresolved y escalar."
  echo "4. Regenerar esta proyección después de cada transición relevante."
  echo ""
  echo "---"
  echo "Regenerar: \`./scripts/update_status.sh\`"
} > "$OUT"

echo "Updated: $OUT"

# --- projections/compact-status.yaml: contraparte machine-readable --------
# Sin campo "phase": PAIOS no tiene concepto de fases de proyecto que lo
# respalde (ver decisions/0005-compact-status-projection.md).

print_yaml_list() {
  local key="$1"; shift
  if [[ $# -eq 0 ]]; then
    echo "${key}: []"
  else
    echo "${key}:"
    local it
    for it in "$@"; do
      echo "  - ${it}"
    done
  fi
}

yaml_single_quote() {
  # Escapa comillas simples duplicándolas (regla YAML para scalars '...')
  echo "${1//\'/\'\'}"
}

PROJECT_NAME="$(basename "$ROOT")"

# latest_event: archivo más reciente en state/deltas/
LATEST_DELTA=""
if compgen -G "state/deltas/*.yaml" > /dev/null 2>&1; then
  LATEST_DELTA=$(ls -1t state/deltas/*.yaml | head -n 1)
fi

# blockers: task ids en state/queue/blocked/
BLOCKERS=()
if compgen -G "state/queue/blocked/*" > /dev/null 2>&1; then
  while IFS= read -r f; do BLOCKERS+=("$(basename "$f")"); done < <(ls -1 state/queue/blocked/)
fi

# pending_approvals: tareas in_progress cuyo hash de zonas protegidas
# divergió de su baseline sin una aprobación humana registrada (reusa
# scripts/lib_protected.sh — el mismo chequeo que hace scripts/verify.sh).
PENDING_APPROVALS=()
if compgen -G "state/queue/in_progress/*" > /dev/null 2>&1; then
  CURRENT_PROTECTED_HASH=$(hash_protected)
  while IFS= read -r f; do
    tid="$(basename "$f")"
    bf="state/baselines/${tid}.sha256"
    [[ -f "$bf" ]] || continue
    baseline_hash=$(head -n 1 "$bf")
    if [[ "$CURRENT_PROTECTED_HASH" != "$baseline_hash" ]]; then
      if compgen -G "state/deltas/*_${tid}_human.yaml" > /dev/null 2>&1 \
         && grep -ql "next_action: approved" state/deltas/*_"${tid}"_human.yaml; then
        : # ya aprobado, no es pending
      else
        PENDING_APPROVALS+=("$tid")
      fi
    fi
  done < <(ls -1 state/queue/in_progress/)
fi

# attempts_warning: 1-2 fallos consecutivos de judge (3+ ya está en blockers)
ATTEMPTS_WARNING=()
if compgen -G "state/attempts/*.count" > /dev/null 2>&1; then
  for f in state/attempts/*.count; do
    tid="$(basename "$f" .count)"
    n="$(cat "$f" 2>/dev/null || echo 0)"
    if [[ "$n" =~ ^[0-9]+$ ]] && [[ "$n" -ge 1 ]] && [[ "$n" -le 2 ]]; then
      ATTEMPTS_WARNING+=("$tid")
    fi
  done
fi

# health + next_action: prioridad pending_approvals > blockers >
# attempts_warning > pending > in_progress > sin trabajo pendiente
if [[ ${#PENDING_APPROVALS[@]} -gt 0 ]]; then
  HEALTH="red"
  NEXT_ACTION="protected paths changed for ${PENDING_APPROVALS[0]}: run ./scripts/approve_gate.sh ${PENDING_APPROVALS[0]} \"reason\" if authorized"
elif [[ ${#BLOCKERS[@]} -gt 0 ]]; then
  HEALTH="red"
  NEXT_ACTION="${BLOCKERS[0]} escalated: run ./scripts/unblock.sh ${BLOCKERS[0]} \"reason\" after investigating"
elif [[ ${#ATTEMPTS_WARNING[@]} -gt 0 ]]; then
  HEALTH="yellow"
  NEXT_ACTION="judge failing for ${ATTEMPTS_WARNING[0]} (see state/attempts/${ATTEMPTS_WARNING[0]}.count) — investigate before it escalates to blocked"
elif [[ "$(count_dir state/queue/pending)" -gt 0 ]]; then
  HEALTH="green"
  FIRST_PENDING="$(ls -1 state/queue/pending/ | head -n 1)"
  NEXT_ACTION="claim ${FIRST_PENDING}: run ./scripts/claim.sh ${FIRST_PENDING}"
elif [[ "$(count_dir state/queue/in_progress)" -gt 0 ]]; then
  HEALTH="green"
  FIRST_IN_PROGRESS="$(ls -1 state/queue/in_progress/ | head -n 1)"
  NEXT_ACTION="continue ${FIRST_IN_PROGRESS}"
else
  HEALTH="green"
  NEXT_ACTION="no pending work"
fi

{
  echo "project: ${PROJECT_NAME}"
  echo "generated: \"${TS}\""
  if [[ -n "$LATEST_DELTA" ]]; then
    echo "latest_event:"
    echo "  task: $(grep '^task:' "$LATEST_DELTA" | head -n1 | sed 's/^task: *//')"
    echo "  agent: $(grep '^agent:' "$LATEST_DELTA" | head -n1 | sed 's/^agent: *//')"
    echo "  timestamp: \"$(grep '^timestamp:' "$LATEST_DELTA" | head -n1 | sed 's/^timestamp: *//')\""
  else
    echo "latest_event: null"
  fi
  echo "queue:"
  echo "  pending: $(count_dir state/queue/pending)"
  echo "  in_progress: $(count_dir state/queue/in_progress)"
  echo "  blocked: $(count_dir state/queue/blocked)"
  echo "  done: $(count_dir state/queue/done)"
  print_yaml_list "blockers" "${BLOCKERS[@]}"
  print_yaml_list "pending_approvals" "${PENDING_APPROVALS[@]}"
  print_yaml_list "attempts_warning" "${ATTEMPTS_WARNING[@]}"
  echo "health: ${HEALTH}"
  echo "next_action: '$(yaml_single_quote "$NEXT_ACTION")'"
} > "$OUT_COMPACT"

echo "Updated: $OUT_COMPACT"
