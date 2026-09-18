#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-current}"

pass() { echo "PASS: $1"; exit 0; }
fail() { echo "FAIL: $1"; exit 1; }

# Valida sintaxis YAML si python+PyYAML están disponibles; si no, degrada a
# un chequeo mínimo (no vacío) en vez de fallar por falta de una herramienta
# externa opcional. No es una dependencia dura del proyecto.
HAVE_PYYAML=0
if command -v python >/dev/null 2>&1 && python -c "import yaml" >/dev/null 2>&1; then
  HAVE_PYYAML=1
elif command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" >/dev/null 2>&1; then
  HAVE_PYYAML=1
fi

validate_yaml_glob() {
  local pattern="$1" f
  compgen -G "$pattern" > /dev/null 2>&1 || return 0
  for f in $pattern; do
    if [[ $HAVE_PYYAML -eq 1 ]]; then
      python -c "import sys,yaml; yaml.safe_load(open(sys.argv[1], encoding='utf-8'))" "$f" 2>/dev/null \
        || python3 -c "import sys,yaml; yaml.safe_load(open(sys.argv[1], encoding='utf-8'))" "$f" \
        || fail "invalid YAML syntax: $f"
    else
      [[ -s "$f" ]] || fail "empty YAML file: $f"
    fi
  done
}

case "$MODE" in
  known_good)
    # Debe pasar siempre. Sirve para verificar que el judge no está roto.
    pass "known_good"
    ;;

  intentionally_broken)
    # Debe fallar siempre. Evita judges decorativos.
    fail "intentionally_broken"
    ;;

  current)
    # Checks estructurales del repo
    [[ -f AGENTS.md ]]                  || fail "missing AGENTS.md"
    [[ -f contracts/project.md ]]       || fail "missing contracts/project.md"
    [[ -f contracts/rulebook.md ]]      || fail "missing contracts/rulebook.md"
    [[ -f contracts/action-gating.md ]] || fail "missing contracts/action-gating.md"
    [[ -f scripts/lib_protected.sh ]]   || fail "missing scripts/lib_protected.sh"
    [[ -d state/deltas ]]               || fail "missing state/deltas/"
    [[ -d state/queue/pending ]]        || fail "missing state/queue/pending/"
    [[ -d state/baselines ]]            || fail "missing state/baselines/"
    [[ -d state/attempts ]]             || fail "missing state/attempts/"
    [[ -x scripts/emit_delta.sh ]]      || fail "scripts/emit_delta.sh not executable"
    [[ -x scripts/queue.sh ]]           || fail "scripts/queue.sh not executable"
    [[ -x scripts/claim.sh ]]           || fail "scripts/claim.sh not executable"
    [[ -x scripts/verify.sh ]]          || fail "scripts/verify.sh not executable"
    [[ -x scripts/update_status.sh ]]   || fail "scripts/update_status.sh not executable"
    [[ -x scripts/approve_gate.sh ]]    || fail "scripts/approve_gate.sh not executable"
    [[ -x scripts/unblock.sh ]]         || fail "scripts/unblock.sh not executable"

    # Contenido real, no solo existencia: sintaxis de los YAML que son
    # fuente de verdad operativa.
    validate_yaml_glob "tasks/*.yaml"
    validate_yaml_glob "state/deltas/*.yaml"
    validate_yaml_glob "state/verification/*.yaml"

    # Lint opcional de los scripts (no es dependencia dura del proyecto).
    if command -v shellcheck >/dev/null 2>&1; then
      shellcheck scripts/*.sh || fail "shellcheck reported issues in scripts/"
    else
      echo "NOTE: shellcheck not installed, skipping script lint" >&2
    fi

    pass "current"
    ;;

  *)
    echo "Usage: $0 {known_good|intentionally_broken|current}"
    exit 2
    ;;
esac
