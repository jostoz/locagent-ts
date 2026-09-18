#!/usr/bin/env bash
set -euo pipefail

TASK_ID="${1:-}"
if [[ -z "$TASK_ID" ]]; then
  echo "Usage: $0 TASK-ID"
  exit 2
fi

mkdir -p state/queue/pending state/queue/in_progress state/queue/blocked state/queue/done state/baselines

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib_protected.sh
source "$ROOT/scripts/lib_protected.sh"

# Buscar la tarea en pending o en tasks/
if [[ -f "state/queue/pending/${TASK_ID}" ]]; then
  mv "state/queue/pending/${TASK_ID}" "state/queue/in_progress/${TASK_ID}"
  echo "Claimed ${TASK_ID} from pending → in_progress"
elif [[ -f "tasks/${TASK_ID}.yaml" ]]; then
  cp "tasks/${TASK_ID}.yaml" "state/queue/in_progress/${TASK_ID}"
  echo "Claimed ${TASK_ID} from tasks/ → in_progress"
else
  echo "ERROR: task ${TASK_ID} not found in pending or tasks/"
  exit 1
fi

# Snapshot de zonas protegidas: verify.sh comparará contra esto para detectar
# cambios no autorizados a contracts/, decisions/, AGENTS.md o los propios
# judge.sh/verify.sh mientras la tarea estuvo en curso.
BASELINE_FILE="state/baselines/${TASK_ID}.sha256"
{
  hash_protected
  echo "# claimed_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
} > "$BASELINE_FILE"
echo "Protected-paths baseline recorded: $BASELINE_FILE"

# Ref de git al momento del claim: si la tarea declara authority.can_modify,
# verify.sh usa esto para calcular qué archivos cambiaron (git diff), sin
# reinventar un sistema de manifest/hashing propio. Silencioso si no es un
# repo git (no bloquea el claim).
if git rev-parse HEAD > /dev/null 2>&1; then
  git rev-parse HEAD > "state/baselines/${TASK_ID}.git_ref"

  # Snapshot de archivos sin trackear: distingue cruft que ya estaba suelto
  # en el working tree de lo que crea la tarea (TASK-008, hallazgo de
  # TASK-007 — un archivo local del harness bloqueó authority.can_modify sin
  # que la tarea lo hubiera tocado).
  git ls-files --others --exclude-standard > "state/baselines/${TASK_ID}.untracked_baseline"
fi

# Hash del bloque authority.can_modify (si la tarea lo declara): verify.sh lo
# recalcula y exige aprobación humana si cambió desde el claim (H6,
# TASK-008) — mismo patrón que el baseline de zonas protegidas de arriba,
# aplicado al task packet en vez de a contracts/decisions/AGENTS.md.
TASK_FILE_FOR_AUTH="tasks/${TASK_ID}.yaml"
if [[ -f "$TASK_FILE_FOR_AUTH" ]]; then
  hash_authority_block "$TASK_FILE_FOR_AUTH" > "state/baselines/${TASK_ID}.authority.sha256"
fi
