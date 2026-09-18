#!/usr/bin/env bash
set -euo pipefail

# Contraparte de claim_worktree.sh (ADR-0017): verifica DENTRO del
# worktree aislado (verify.sh sin modificar), y solo si acepta, commitea
# lo que haya quedado sin commitear ahi (claim.sh/verify.sh escriben
# archivos, no commitean) y lo mergea al repo principal. Si verify.sh
# rechaza, no toca el repo principal -- el worktree queda intacto para
# seguir corrigiendo.

TASK_ID="${1:-}"
if [[ -z "$TASK_ID" ]]; then
  echo "Usage: $0 TASK-ID"
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REPO_NAME="$(basename "$ROOT")"
WORKTREE_DIR="$(cd "$ROOT/.." && pwd)/${REPO_NAME}-worktrees/${TASK_ID}"
BRANCH="task/${TASK_ID}"

if [[ ! -d "$WORKTREE_DIR" ]]; then
  echo "ERROR: no existe worktree en $WORKTREE_DIR -- ¿se reclamo con claim_worktree.sh?" >&2
  exit 1
fi

echo "=== Verificando dentro del worktree ($WORKTREE_DIR) ==="
if ! (cd "$WORKTREE_DIR" && bash scripts/verify.sh "$TASK_ID"); then
  echo "" >&2
  echo "RECHAZADO: verify.sh no acepto ${TASK_ID} dentro del worktree." >&2
  echo "El worktree queda intacto en $WORKTREE_DIR para seguir corrigiendo." >&2
  exit 1
fi

# claim.sh/verify.sh solo escriben archivos, no commitean -- sin un commit
# en la rama del worktree, git merge no tendria nada que traer.
if [[ -n "$(cd "$WORKTREE_DIR" && git status --porcelain)" ]]; then
  echo ""
  echo "=== Commiteando el cierre del worktree ==="
  (cd "$WORKTREE_DIR" && git add -A && git commit -q -m "${TASK_ID}: cierre de worktree aislado (ADR-0017)")
fi

echo ""
echo "=== Mergeando ${BRANCH} en $(git rev-parse --abbrev-ref HEAD) ==="
git merge "$BRANCH" --no-edit

echo ""
echo "=== Limpiando worktree ==="
# git worktree remove puede fallar en Windows por un lock transitorio del
# directorio (un proceso -incluso este mismo shell si quedó con cwd
# adentro- lo tiene abierto un instante). El merge YA ocurrió antes de
# este punto -- no hay que abortar el script entero por un fallo de
# limpieza, solo avisar. git desregistra el worktree igual aunque falle
# el borrado físico del directorio, así que el borrado de rama se
# intenta de todas formas, no solo en el camino feliz.
REMOVE_ERR="$(mktemp)"
if ! git worktree remove "$WORKTREE_DIR" 2>"$REMOVE_ERR"; then
  echo "WARN: no se pudo remover el worktree automáticamente (lock de Windows probable):" >&2
  cat "$REMOVE_ERR" >&2
  echo "      El merge ya se aplicó -- esto es solo limpieza pendiente." >&2
  echo "      Reintentá en unos segundos: git worktree remove \"$WORKTREE_DIR\"" >&2
  echo "      (o borrá el directorio a mano y \`git worktree prune\`)." >&2
fi
rm -f "$REMOVE_ERR"
git worktree prune
git branch -d "$BRANCH" 2>/dev/null || echo "WARN: no se pudo borrar la rama ${BRANCH} todavía (reintentar tras limpiar el worktree)." >&2

echo ""
echo "Mergeado. ${TASK_ID} ahora en el repo principal."
