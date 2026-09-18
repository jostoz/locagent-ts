#!/usr/bin/env bash
set -euo pipefail

# Reconstruye la vista de cola desde el filesystem.
# No depende de memoria conversacional.

echo "=== PENDING ==="
if compgen -G "tasks/*.yaml" > /dev/null; then
  for task in tasks/*.yaml; do
    id=$(basename "$task" .yaml)
    if [[ ! -f "state/queue/done/${id}" && ! -f "state/queue/in_progress/${id}" && ! -f "state/queue/blocked/${id}" ]]; then
      echo "$id"
      # Asegurar que exista en pending (idempotente)
      mkdir -p state/queue/pending
      [[ -f "state/queue/pending/${id}" ]] || cp "$task" "state/queue/pending/${id}" 2>/dev/null || touch "state/queue/pending/${id}"
    fi
  done
else
  echo "(ninguna tarea en tasks/)"
fi

echo ""
echo "=== IN_PROGRESS ==="
ls -1 state/queue/in_progress/ 2>/dev/null || echo "(vacío)"

echo ""
echo "=== BLOCKED ==="
ls -1 state/queue/blocked/ 2>/dev/null || echo "(vacío)"

echo ""
echo "=== DONE ==="
if compgen -G "state/queue/done/*" > /dev/null; then
  for done_file in state/queue/done/*; do
    id=$(basename "$done_file")
    # La fecha de verificación vive en state/verification/<id>_<ts>.yaml
    # (campo `timestamp:`). Si se re-verificó hay varios; el glob se expande
    # ordenado y el timestamp UTC ordena cronológicamente, así que el último
    # es el más reciente.
    vfile=""
    for cand in state/verification/"${id}"_*.yaml; do
      [[ -e "$cand" ]] && vfile="$cand"
    done
    if [[ -n "$vfile" ]]; then
      verified=$(sed -n 's/^timestamp:[[:space:]]*//p' "$vfile" | head -n 1)
      echo "${id}  (verificada: ${verified:-fecha no registrada})"
    else
      echo "${id}  (sin registro en state/verification/)"
    fi
  done
else
  echo "(vacío)"
fi
