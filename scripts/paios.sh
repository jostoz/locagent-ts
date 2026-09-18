#!/usr/bin/env bash
set -euo pipefail

# Punto de entrada único para los scripts de PAIOS. Despacha a los scripts
# individuales sin agregar lógica propia (así no duplica reglas que ya
# viven en cada script). Asume, como el resto, que se invoca desde la raíz
# del repo.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

usage() {
  cat <<'USAGE'
Usage: scripts/paios.sh <command> [args...]

Commands:
  queue                       Reconciliar y listar la cola             (queue.sh)
  claim TASK-ID                Reclamar una tarea                       (claim.sh)
  delta TASK-ID AGENT "msg"    Emitir un state delta                    (emit_delta.sh)
  judge [mode]                 Correr el judge (default: current)       (judge.sh)
  verify TASK-ID                Verificar y cerrar una tarea             (verify.sh)
  approve TASK-ID "reason"     Aprobar cambio a zona protegida          (approve_gate.sh)
  unblock TASK-ID "reason"     Desbloquear una tarea escalada           (unblock.sh)
  status                       Regenerar projections/status.md          (update_status.sh)
  test                         Correr los tests locales                 (run_tests.sh)
  help                         Mostrar esta ayuda

Todos los comandos asumen que se invoca desde la raíz del repo (o vía
scripts/paios.sh, que se reubica ahí solo).
USAGE
}

CMD="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$CMD" in
  queue)   ./scripts/queue.sh "$@" ;;
  claim)   ./scripts/claim.sh "$@" ;;
  delta)   ./scripts/emit_delta.sh "$@" ;;
  judge)   ./scripts/judge.sh "$@" ;;
  verify)  ./scripts/verify.sh "$@" ;;
  approve) ./scripts/approve_gate.sh "$@" ;;
  unblock) ./scripts/unblock.sh "$@" ;;
  status)  ./scripts/update_status.sh "$@" ;;
  test)    ./scripts/run_tests.sh "$@" ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown command: $CMD" >&2
    usage
    exit 2
    ;;
esac
