#!/usr/bin/env bash
set -euo pipefail

# Aislamiento real de concurrencia (ADR-0017): en vez de claim.sh directo
# sobre el working tree compartido, crea un git worktree nuevo para la
# tarea y reclama ahi adentro. claim.sh no se modifica -- ya opera
# enteramente via git (ver scripts/lib_protected.sh), asi que corre sin
# cambios dentro de un worktree distinto.

TASK_ID="${1:-}"
if [[ -z "$TASK_ID" ]]; then
  echo "Usage: $0 TASK-ID"
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Un worktree nuevo solo ve contenido committeado -- si la tarea se
# escribio recien y no se committeo, el worktree no la va a encontrar y
# claim.sh fallaria ahi con un mensaje confuso. Se valida antes, claro.
if ! git cat-file -e "HEAD:tasks/${TASK_ID}.yaml" 2>/dev/null \
   && ! git cat-file -e "HEAD:state/queue/pending/${TASK_ID}" 2>/dev/null; then
  echo "ERROR: ${TASK_ID} no esta committeada en HEAD" >&2
  echo "       (ni tasks/${TASK_ID}.yaml ni state/queue/pending/${TASK_ID})." >&2
  echo "       Un worktree nuevo solo ve contenido committeado -- commiteala primero." >&2
  exit 1
fi

REPO_NAME="$(basename "$ROOT")"
WORKTREE_DIR="$(cd "$ROOT/.." && pwd)/${REPO_NAME}-worktrees/${TASK_ID}"
BRANCH="task/${TASK_ID}"

if [[ -e "$WORKTREE_DIR" ]]; then
  echo "ERROR: ya existe $WORKTREE_DIR -- ¿tarea ya reclamada en worktree?" >&2
  echo "       Revisa: git worktree list" >&2
  exit 1
fi

mkdir -p "$(dirname "$WORKTREE_DIR")"
git worktree add "$WORKTREE_DIR" -b "$BRANCH" HEAD

echo "Worktree creado: $WORKTREE_DIR (rama ${BRANCH})"
echo "Reclamando dentro del worktree..."
(cd "$WORKTREE_DIR" && bash scripts/claim.sh "$TASK_ID")

echo ""
echo "Trabaja en: $WORKTREE_DIR"
echo "Cuando termines: bash scripts/merge_worktree.sh ${TASK_ID}"
