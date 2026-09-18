#!/usr/bin/env bash
set -euo pipefail

# Tests locales del pipeline PAIOS (sin CI remoto — ver ADR-0003).
# Corren sobre una copia aislada en un directorio temporal para no tocar
# el estado real del repo (tasks/, state/).

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS_COUNT=0
FAIL_COUNT=0

ok()  { echo "  PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
bad() { echo "  FAIL: $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

# --- Sandbox: copia mínima con estado operativo limpio ---------------------
# No copiar el checkout entero: contiene .venv/, caches BM25 y resultados de
# evaluaciones que no participan de estos tests y pueden ocupar gigabytes.
cp "$ROOT/AGENTS.md" "$TMP/"
cp -R "$ROOT/contracts" "$ROOT/scripts" "$TMP/"
mkdir -p "$TMP/tasks" "$TMP/projections"
mkdir -p "$TMP"/state/queue/pending "$TMP"/state/queue/in_progress "$TMP"/state/queue/blocked "$TMP"/state/queue/done \
  "$TMP"/state/deltas "$TMP"/state/reviews "$TMP"/state/verification "$TMP"/state/baselines "$TMP"/state/attempts
cd "$TMP"
chmod +x scripts/*.sh

echo "=== Grupo A: judge.sh ==="
if ./scripts/judge.sh known_good >/dev/null 2>&1; then ok "known_good → PASS"; else bad "known_good debería pasar"; fi
if ./scripts/judge.sh intentionally_broken >/dev/null 2>&1; then bad "intentionally_broken no debería pasar"; else ok "intentionally_broken → FAIL real"; fi
if ./scripts/judge.sh current >/dev/null 2>&1; then ok "current → PASS con estructura completa"; else bad "current debería pasar con estructura completa"; fi

echo ""
echo "=== Grupo B: ciclo feliz TASK-T1 (claim → delta → review → verify) ==="
cat > tasks/TASK-T1.yaml <<'YAML'
id: TASK-T1
title: fixture de test
objective: probar el ciclo feliz
acceptance:
  - n/a
priority: low
status: pending
YAML

# Nota: capturar primero y grepear después (no "queue.sh | grep -q") — con
# pipefail activo, grep -q corta el pipe en el primer match y queue.sh recibe
# SIGPIPE al seguir escribiendo, lo que reporta fallo aunque el match existió.
QUEUE_OUT=$(./scripts/queue.sh 2>&1)
if echo "$QUEUE_OUT" | grep -q "TASK-T1"; then ok "queue.sh descubre TASK-T1 desde tasks/"; else bad "queue.sh debería listar TASK-T1"; fi

./scripts/claim.sh TASK-T1 >/dev/null
if [[ -f state/queue/in_progress/TASK-T1 && -f state/baselines/TASK-T1.sha256 ]]; then
  ok "claim.sh mueve a in_progress y registra baseline"
else
  bad "claim.sh debería crear in_progress + baseline"
fi

if ./scripts/verify.sh TASK-T1 >/dev/null 2>&1; then bad "verify.sh no debería aceptar sin delta"; else ok "verify.sh rechaza sin delta"; fi

./scripts/emit_delta.sh TASK-T1 builder "trabajo de prueba" >/dev/null

if ./scripts/verify.sh TASK-T1 >/dev/null 2>&1; then bad "verify.sh no debería aceptar sin review"; else ok "verify.sh rechaza sin review"; fi

touch "state/reviews/TASK-T1_review.md"
if ./scripts/verify.sh TASK-T1 >/dev/null 2>&1; then bad "verify.sh no debería aceptar review vacía"; else ok "verify.sh rechaza review vacía"; fi

cat > "state/reviews/TASK-T1_review.md" <<'MD'
RULE: contracts/rulebook.md#1
ISSUE: none
PASS
MD
if ./scripts/verify.sh TASK-T1 >/dev/null 2>&1 && [[ -f state/queue/done/TASK-T1 ]]; then
  ok "verify.sh acepta con delta + review válida → done"
else
  bad "verify.sh debería aceptar con delta + review válida"
fi

echo ""
echo "=== Grupo C: integridad de zonas protegidas (enforcement de action-gating) ==="
cat > tasks/TASK-T2.yaml <<'YAML'
id: TASK-T2
title: fixture de tamper de zonas protegidas
objective: probar que verify.sh detecta cambios no autorizados
acceptance:
  - n/a
priority: low
status: pending
YAML

./scripts/claim.sh TASK-T2 >/dev/null
./scripts/emit_delta.sh TASK-T2 builder "trabajo de prueba" >/dev/null
cat > "state/reviews/TASK-T2_review.md" <<'MD'
RULE: contracts/rulebook.md#1
ISSUE: none
PASS
MD

# Simular que el builder tocó una zona protegida sin autorización
echo "<!-- tamper de prueba -->" >> contracts/rulebook.md

if ./scripts/verify.sh TASK-T2 >/dev/null 2>&1; then
  bad "verify.sh debería rechazar cambios no autorizados a zonas protegidas"
else
  ok "verify.sh rechaza tamper de zona protegida sin aprobación humana"
fi

./scripts/approve_gate.sh TASK-T2 "aprobación de prueba" >/dev/null

if ./scripts/verify.sh TASK-T2 >/dev/null 2>&1 && [[ -f state/queue/done/TASK-T2 ]]; then
  ok "verify.sh acepta tras aprobación humana explícita (approve_gate.sh)"
else
  bad "verify.sh debería aceptar tras approve_gate.sh"
fi

echo ""
echo "=== Grupo D: escalación por fallos consecutivos de judge (cierre del loop, ADR-0004) ==="
cat > tasks/TASK-T3.yaml <<'YAML'
id: TASK-T3
title: fixture de escalacion
objective: probar que 3 fallos de judge escalan a blocked
acceptance:
  - n/a
priority: low
status: pending
YAML

./scripts/claim.sh TASK-T3 >/dev/null
./scripts/emit_delta.sh TASK-T3 builder "trabajo de prueba" >/dev/null
cat > "state/reviews/TASK-T3_review.md" <<'MD'
RULE: contracts/rulebook.md#1
ISSUE: none
PASS
MD

# Romper judge.sh current de forma determinista (falta un archivo requerido)
mv contracts/action-gating.md contracts/action-gating.md.bak

if ./scripts/verify.sh TASK-T3 >/dev/null 2>&1; then R1=0; else R1=1; fi
if ./scripts/verify.sh TASK-T3 >/dev/null 2>&1; then R2=0; else R2=1; fi
if ./scripts/verify.sh TASK-T3 >/dev/null 2>&1; then R3=0; else R3=1; fi

if [[ $R1 -ne 0 && $R2 -ne 0 && $R3 -ne 0 && -f state/queue/blocked/TASK-T3 && ! -f state/queue/in_progress/TASK-T3 ]]; then
  ok "3 fallos consecutivos de judge escalan TASK-T3 a blocked/"
else
  bad "TASK-T3 debería estar en blocked/ tras 3 fallos consecutivos (R1=$R1 R2=$R2 R3=$R3)"
fi

if compgen -G "state/deltas/*_TASK-T3_verifier*.yaml" > /dev/null 2>&1 \
   && grep -ql "next_action: escalate" state/deltas/*_TASK-T3_verifier*.yaml; then
  ok "delta de escalación emitido con next_action: escalate"
else
  bad "debería existir un delta de escalación con next_action: escalate"
fi

# Cada intento fallido (no solo el que escala) debe dejar su propio delta
# durable, con la salida real de judge como evidencia (ADR-0008).
DELTA_COUNT=$(ls -1 state/deltas/*_TASK-T3_verifier*.yaml 2>/dev/null | wc -l | tr -d ' ')
if [[ "$DELTA_COUNT" -eq 3 ]]; then
  ok "cada intento fallido de judge dejó su propio delta (3 deltas para 3 intentos)"
else
  bad "deberían existir 3 deltas verifier para TASK-T3 (uno por intento), hay $DELTA_COUNT"
fi

RETRY_COUNT=$(grep -l "next_action: retry" state/deltas/*_TASK-T3_verifier*.yaml 2>/dev/null | wc -l | tr -d ' ')
if [[ "$RETRY_COUNT" -eq 2 ]]; then
  ok "intentos 1 y 2 quedaron con next_action: retry (ya no se pierden en stdout)"
else
  bad "deberían existir 2 deltas con next_action: retry, hay $RETRY_COUNT"
fi

if grep -rq "judge_output:" state/deltas/*_TASK-T3_verifier*.yaml 2>/dev/null \
   && grep -rq "missing contracts/action-gating.md" state/deltas/*_TASK-T3_verifier*.yaml 2>/dev/null; then
  ok "el delta incluye la salida real de judge.sh como evidencia (judge_output)"
else
  bad "el delta debería incluir la salida real de judge (judge_output con el motivo real del fallo)"
fi

# Restaurar el archivo roto y desbloquear
mv contracts/action-gating.md.bak contracts/action-gating.md
./scripts/unblock.sh TASK-T3 "archivo restaurado, reintentar" >/dev/null

if [[ -f state/queue/pending/TASK-T3 && ! -f state/attempts/TASK-T3.count ]]; then
  ok "unblock.sh devuelve TASK-T3 a pending/ y reinicia el contador de intentos"
else
  bad "unblock.sh debería devolver TASK-T3 a pending/ y borrar el contador"
fi

./scripts/claim.sh TASK-T3 >/dev/null
if ./scripts/verify.sh TASK-T3 >/dev/null 2>&1 && [[ -f state/queue/done/TASK-T3 ]]; then
  ok "TASK-T3 se completa tras el ciclo escalate → unblock → retry"
else
  bad "TASK-T3 debería poder completarse tras unblock.sh"
fi

echo ""
echo "=== Grupo E: paios CLI wrapper (dispatch + quoting, TASK-003) ==="

if ./scripts/paios.sh badcommand >/dev/null 2>&1; then
  bad "paios.sh con comando desconocido debería fallar"
else
  ok "paios.sh rechaza comandos desconocidos con exit != 0"
fi

QUEUE_A=$(./scripts/queue.sh 2>&1)
QUEUE_B=$(./scripts/paios.sh queue 2>&1)
if [[ "$QUEUE_A" == "$QUEUE_B" ]]; then
  ok "paios.sh queue despacha igual que queue.sh"
else
  bad "paios.sh queue debería producir la misma salida que queue.sh"
fi

cat > tasks/TASK-CLI1.yaml <<'YAML'
id: TASK-CLI1
title: fixture del CLI wrapper
objective: probar dispatch y quoting de paios.sh
acceptance:
  - n/a
priority: low
status: pending
YAML

./scripts/paios.sh claim TASK-CLI1 >/dev/null
if [[ -f state/queue/in_progress/TASK-CLI1 && -f state/baselines/TASK-CLI1.sha256 ]]; then
  ok "paios.sh claim despacha a claim.sh (in_progress + baseline)"
else
  bad "paios.sh claim debería crear in_progress + baseline"
fi

MSG_WITH_SPACES="mensaje con espacios y \"algo\" de puntuación"
./scripts/paios.sh delta TASK-CLI1 builder "$MSG_WITH_SPACES" >/dev/null
DELTA_FILE=$(ls -1t state/deltas/*_TASK-CLI1_builder.yaml 2>/dev/null | head -n 1)
if [[ -n "$DELTA_FILE" ]] && grep -q "espacios y" "$DELTA_FILE"; then
  ok "paios.sh delta preserva un mensaje con espacios/comillas sin corromper el quoting"
else
  bad "paios.sh delta debería preservar el mensaje completo con espacios"
fi

cat > "state/reviews/TASK-CLI1_review.md" <<'MD'
RULE: contracts/rulebook.md#1
ISSUE: none
PASS
MD

if ./scripts/paios.sh verify TASK-CLI1 >/dev/null 2>&1 && [[ -f state/queue/done/TASK-CLI1 ]]; then
  ok "paios.sh verify despacha a verify.sh y cierra la tarea"
else
  bad "paios.sh verify debería aceptar y mover TASK-CLI1 a done"
fi

echo ""
echo "=== Grupo F: compact-status.yaml (proyección machine-readable, TASK-004) ==="

./scripts/update_status.sh >/dev/null

if [[ -f projections/compact-status.yaml ]]; then
  ok "compact-status.yaml se genera"
else
  bad "compact-status.yaml debería generarse"
fi

COMPACT=$(cat projections/compact-status.yaml)
MISSING_KEYS=""
for key in project generated latest_event queue blockers pending_approvals attempts_warning health next_action; do
  if ! echo "$COMPACT" | grep -q "^${key}:"; then
    MISSING_KEYS="${MISSING_KEYS} ${key}"
  fi
done
if [[ -z "$MISSING_KEYS" ]]; then
  ok "compact-status.yaml contiene todas las claves esperadas"
else
  bad "faltan claves en compact-status.yaml:${MISSING_KEYS}"
fi

if echo "$COMPACT" | grep -q "^  done: 4$" \
   && echo "$COMPACT" | grep -q "^health: green$" \
   && echo "$COMPACT" | grep -q "no pending work"; then
  ok "compact-status.yaml refleja el estado real del sandbox (4 done, health green, sin trabajo pendiente)"
else
  bad "compact-status.yaml debería reflejar 4 tareas done y health green en este punto"
fi

# pending_approvals debe reflejar divergencias reales de zona protegida vía
# lib_protected.sh — no simplemente el contenido de state/queue/blocked/
cat > tasks/TASK-CLI2.yaml <<'YAML'
id: TASK-CLI2
title: fixture de pending_approvals
objective: probar que compact-status detecta zonas protegidas sin aprobar
acceptance:
  - n/a
priority: low
status: pending
YAML
./scripts/claim.sh TASK-CLI2 >/dev/null
echo "<!-- tamper para compact-status -->" >> contracts/rulebook.md
./scripts/update_status.sh >/dev/null

if grep -q "TASK-CLI2" projections/compact-status.yaml && grep -q "^health: red$" projections/compact-status.yaml; then
  ok "compact-status.yaml detecta pending_approvals real vía lib_protected.sh (no vía blocked/)"
else
  bad "compact-status.yaml debería listar TASK-CLI2 en pending_approvals con health red"
fi

./scripts/approve_gate.sh TASK-CLI2 "aprobación de prueba para compact-status" >/dev/null
./scripts/update_status.sh >/dev/null

if grep -q "^pending_approvals: \[\]$" projections/compact-status.yaml; then
  ok "compact-status.yaml limpia pending_approvals tras approve_gate.sh"
else
  bad "pending_approvals debería quedar vacío tras la aprobación"
fi

echo ""
echo "=== Grupo G: authority.can_modify por task packet (opt-in, ADR-0006) ==="

# El sandbox no tiene .git (se removió al crearlo); este grupo necesita uno
# real para que changed_files_since_claim pueda usar git diff.
git init -q
git add -A
git commit -q -m "sandbox baseline for authority tests"

# Caso 1: tarea SIN authority -> sin restricción extra (no debe romper lo existente)
cat > tasks/TASK-AUTH1.yaml <<'YAML'
id: TASK-AUTH1
title: fixture sin authority
objective: confirmar que tareas sin authority no cambian de comportamiento
acceptance:
  - n/a
priority: low
status: pending
YAML
./scripts/claim.sh TASK-AUTH1 >/dev/null
mkdir -p anywhere
echo "contenido" > anywhere/archivo.txt
./scripts/emit_delta.sh TASK-AUTH1 builder "trabajo sin authority declarado" >/dev/null
cat > "state/reviews/TASK-AUTH1_review.md" <<'MD'
RULE: contracts/rulebook.md#1
ISSUE: none
PASS
MD
if ./scripts/verify.sh TASK-AUTH1 >/dev/null 2>&1 && [[ -f state/queue/done/TASK-AUTH1 ]]; then
  ok "tarea sin authority: sin restricción extra (comportamiento igual que antes)"
else
  bad "tarea sin authority debería aceptar cambios en cualquier ruta no protegida"
fi
git add -A && git commit -q -m "after TASK-AUTH1"

# Caso 2: tarea CON authority.can_modify, cambio dentro del scope -> acepta
cat > tasks/TASK-AUTH2.yaml <<'YAML'
id: TASK-AUTH2
title: fixture con authority, dentro de scope
objective: confirmar que cambios dentro de can_modify se aceptan
authority:
  can_modify:
    - allowed/**
acceptance:
  - n/a
priority: low
status: pending
YAML
./scripts/claim.sh TASK-AUTH2 >/dev/null
mkdir -p allowed
echo "contenido permitido" > allowed/archivo.txt
./scripts/emit_delta.sh TASK-AUTH2 builder "trabajo dentro de scope" >/dev/null
cat > "state/reviews/TASK-AUTH2_review.md" <<'MD'
RULE: contracts/rulebook.md#1
ISSUE: none
PASS
MD
if ./scripts/verify.sh TASK-AUTH2 >/dev/null 2>&1 && [[ -f state/queue/done/TASK-AUTH2 ]]; then
  ok "tarea con authority.can_modify: acepta cambios dentro del scope declarado"
else
  bad "debería aceptar cambios dentro de authority.can_modify"
fi
git add -A && git commit -q -m "after TASK-AUTH2"

# Caso 3: tarea CON authority.can_modify, cambio FUERA de scope -> rechaza
cat > tasks/TASK-AUTH3.yaml <<'YAML'
id: TASK-AUTH3
title: fixture con authority, fuera de scope
objective: confirmar que cambios fuera de can_modify se rechazan
authority:
  can_modify:
    - allowed/**
acceptance:
  - n/a
priority: low
status: pending
YAML
./scripts/claim.sh TASK-AUTH3 >/dev/null
mkdir -p forbidden
echo "no debería estar aquí" > forbidden/archivo.txt
./scripts/emit_delta.sh TASK-AUTH3 builder "trabajo fuera de scope" >/dev/null
cat > "state/reviews/TASK-AUTH3_review.md" <<'MD'
RULE: contracts/rulebook.md#1
ISSUE: none
PASS
MD
if VERIFY_OUT=$(./scripts/verify.sh TASK-AUTH3 2>&1); then VERIFY_RC=0; else VERIFY_RC=$?; fi
if [[ $VERIFY_RC -ne 0 ]] && echo "$VERIFY_OUT" | grep -q "forbidden/archivo.txt"; then
  ok "tarea con authority.can_modify: rechaza cambio fuera de scope y lo lista"
else
  bad "debería rechazar forbidden/archivo.txt fuera de authority.can_modify (rc=$VERIFY_RC)"
fi

echo ""
echo "=== Grupo H: request_approval.sh (cierra el gap de contracts/action-gating.md) ==="

cat > tasks/TASK-APR1.yaml <<'YAML'
id: TASK-APR1
title: fixture de request_approval
objective: probar que request_approval.sh escribe el delta y bloquea la tarea
acceptance:
  - n/a
priority: low
status: pending
YAML

./scripts/claim.sh TASK-APR1 >/dev/null
if ./scripts/request_approval.sh TASK-APR1 human "no debería aceptar agent=human" >/dev/null 2>&1; then
  bad "request_approval.sh no debería aceptar agent=human"
else
  ok "request_approval.sh rechaza agent=human (approve_gate.sh es lo que otorga, no lo que pide)"
fi

./scripts/request_approval.sh TASK-APR1 builder "modificar contracts/rulebook.md para agregar regla de prueba" >/dev/null

if [[ -f state/queue/blocked/TASK-APR1 && ! -f state/queue/in_progress/TASK-APR1 ]]; then
  ok "request_approval.sh mueve la tarea de in_progress a blocked"
else
  bad "request_approval.sh debería mover TASK-APR1 a blocked/"
fi

DELTA=$(ls -1t state/deltas/*_TASK-APR1_builder.yaml 2>/dev/null | head -n 1)
if [[ -n "$DELTA" ]] && grep -q "next_action: await_human_approval" "$DELTA" && grep -q "reason: action-gating" "$DELTA"; then
  ok "el delta tiene next_action: await_human_approval y reason: action-gating"
else
  bad "el delta debería tener next_action: await_human_approval y reason: action-gating"
fi

# Ciclo completo: aprobar, desbloquear, y confirmar que el ciclo puede cerrar
cp contracts/rulebook.md /tmp/rulebook_backup_apr1.$$
echo "<!-- tamper de prueba para TASK-APR1 -->" >> contracts/rulebook.md
./scripts/approve_gate.sh TASK-APR1 "aprobación de prueba para request_approval" >/dev/null
./scripts/unblock.sh TASK-APR1 "aprobado, reintentar" >/dev/null
# Restaurar de inmediato: sin esto, contracts/rulebook.md queda modificado
# sin commitear en el working tree del sandbox para el resto de la suite,
# y git diff lo reporta como "cambiado" contra el ref de CUALQUIER tarea
# reclamada después -- aunque esa tarea nunca lo haya tocado. Encontrado al
# escribir Grupo J (TASK-008): fue el primer grupo posterior que declaró
# authority.can_modify y de verdad miró el diff completo contra git.
cp /tmp/rulebook_backup_apr1.$$ contracts/rulebook.md
rm -f /tmp/rulebook_backup_apr1.$$

if [[ -f state/queue/pending/TASK-APR1 ]]; then
  ok "tras approve_gate.sh + unblock.sh, TASK-APR1 vuelve a pending/"
else
  bad "TASK-APR1 debería volver a pending/ tras approve_gate.sh + unblock.sh"
fi

./scripts/claim.sh TASK-APR1 >/dev/null
cat > "state/reviews/TASK-APR1_review.md" <<'MD'
RULE: contracts/rulebook.md#1
ISSUE: none
PASS
MD
if ./scripts/verify.sh TASK-APR1 >/dev/null 2>&1 && [[ -f state/queue/done/TASK-APR1 ]]; then
  ok "TASK-APR1 se completa tras el ciclo request_approval -> approve_gate -> unblock -> verify"
else
  bad "TASK-APR1 debería poder completarse tras el ciclo completo de aprobación"
fi

echo ""
echo "=== Grupo I: lib_protected.sh se protege a sí mismo (H1 de TASK-007) ==="

# Sin el arreglo, scripts/lib_protected.sh no estaba en PROTECTED_PATHS: era el
# único archivo desde el cual se podía desactivar el mecanismo entero de forma
# persistente sin que verify.sh lo notara en el siguiente claim.

if grep -q '"scripts/lib_protected.sh"' scripts/lib_protected.sh; then
  ok "lib_protected.sh se incluye a sí mismo en PROTECTED_PATHS"
else
  bad "PROTECTED_PATHS debería incluir scripts/lib_protected.sh"
fi

cat > tasks/TASK-LIB1.yaml <<'YAML'
id: TASK-LIB1
title: fixture de tamper sobre el definidor de zonas protegidas
objective: probar que verify.sh detecta cambios en scripts/lib_protected.sh
acceptance:
  - n/a
priority: low
status: pending
YAML

./scripts/claim.sh TASK-LIB1 >/dev/null
./scripts/emit_delta.sh TASK-LIB1 builder "trabajo de prueba" >/dev/null
cat > "state/reviews/TASK-LIB1_review.md" <<'MD'
RULE: contracts/rulebook.md#1
ISSUE: none
PASS
MD

# El tamper que antes pasaba inadvertido: tocar el archivo que DEFINE la lista.
cp scripts/lib_protected.sh /tmp/lib_protected_backup.$$
echo "# tamper de prueba sobre el definidor de zonas protegidas" >> scripts/lib_protected.sh

if ./scripts/verify.sh TASK-LIB1 >/dev/null 2>&1; then
  bad "verify.sh debería rechazar un tamper de scripts/lib_protected.sh"
else
  ok "verify.sh rechaza tamper de lib_protected.sh (el archivo que define las zonas protegidas)"
fi

./scripts/approve_gate.sh TASK-LIB1 "aprobación de prueba para tamper de lib_protected" >/dev/null

if ./scripts/verify.sh TASK-LIB1 >/dev/null 2>&1 && [[ -f state/queue/done/TASK-LIB1 ]]; then
  ok "verify.sh acepta el cambio de lib_protected.sh tras aprobación humana"
else
  bad "verify.sh debería aceptar el cambio tras approve_gate.sh"
fi

cp /tmp/lib_protected_backup.$$ scripts/lib_protected.sh
rm -f /tmp/lib_protected_backup.$$

echo ""
echo "=== Grupo J: authority.can_modify se protege a sí mismo (H6), cruft preexistente no cuenta (H7, TASK-008) ==="

# H6: el builder no puede ensancharse su propio scope sin aprobación humana.
cat > tasks/TASK-H6.yaml <<'YAML'
id: TASK-H6
title: fixture de auto-ampliacion de authority
objective: probar que verify.sh detecta un authority.can_modify editado sin aprobacion
authority:
  can_modify:
    - allowed-h6/**
acceptance:
  - n/a
priority: low
status: pending
YAML
./scripts/claim.sh TASK-H6 >/dev/null

if [[ -f state/baselines/TASK-H6.authority.sha256 ]]; then
  ok "claim.sh graba el baseline de authority.can_modify"
else
  bad "claim.sh debería grabar state/baselines/TASK-H6.authority.sha256"
fi

mkdir -p forbidden-h6
echo "no deberia estar aqui" > forbidden-h6/archivo.txt
./scripts/emit_delta.sh TASK-H6 builder "trabajo de prueba" >/dev/null
cat > "state/reviews/TASK-H6_review.md" <<'MD'
RULE: contracts/rulebook.md#1
ISSUE: none
PASS
MD

# El builder se ensancha el scope a mano, sin aprobación humana.
cat > tasks/TASK-H6.yaml <<'YAML'
id: TASK-H6
title: fixture de auto-ampliacion de authority
objective: probar que verify.sh detecta un authority.can_modify editado sin aprobacion
authority:
  can_modify:
    - allowed-h6/**
    - forbidden-h6/**
acceptance:
  - n/a
priority: low
status: pending
YAML

if VERIFY_OUT=$(./scripts/verify.sh TASK-H6 2>&1); then VERIFY_RC=0; else VERIFY_RC=$?; fi
if [[ $VERIFY_RC -ne 0 ]] && echo "$VERIFY_OUT" | grep -q "authority.can_modify de tasks/TASK-H6.yaml cambió"; then
  ok "verify.sh rechaza authority.can_modify ensanchado sin aprobación humana"
else
  bad "verify.sh debería rechazar el ensanchamiento de authority.can_modify sin aprobación (rc=$VERIFY_RC)"
fi

./scripts/approve_gate.sh TASK-H6 "aprobacion de prueba para ensanchar authority" >/dev/null

if ./scripts/verify.sh TASK-H6 >/dev/null 2>&1 && [[ -f state/queue/done/TASK-H6 ]]; then
  ok "verify.sh acepta authority.can_modify ensanchado tras aprobación humana"
else
  bad "verify.sh debería aceptar tras approve_gate.sh"
fi

# H7: un archivo sin trackear que ya existía ANTES del claim no cuenta como
# cambio de la tarea (p. ej. config local del harness, como pasó en TASK-007).
echo "cruft preexistente, no relacionada con la tarea" > .stray-cruft-before-claim.txt

cat > tasks/TASK-H7.yaml <<'YAML'
id: TASK-H7
title: fixture de cruft sin trackear preexistente
objective: probar que archivos sueltos previos al claim no cuentan como cambio
authority:
  can_modify:
    - artifacts-h7/**
acceptance:
  - n/a
priority: low
status: pending
YAML
./scripts/claim.sh TASK-H7 >/dev/null

if [[ -f state/baselines/TASK-H7.untracked_baseline ]] && grep -q "stray-cruft-before-claim.txt" state/baselines/TASK-H7.untracked_baseline; then
  ok "claim.sh graba el snapshot de archivos sin trackear preexistentes"
else
  bad "claim.sh debería grabar .stray-cruft-before-claim.txt en el snapshot de untracked"
fi

mkdir -p artifacts-h7
echo "trabajo real" > artifacts-h7/salida.txt
./scripts/emit_delta.sh TASK-H7 builder "trabajo de prueba" >/dev/null
cat > "state/reviews/TASK-H7_review.md" <<'MD'
RULE: contracts/rulebook.md#1
ISSUE: none
PASS
MD

if ./scripts/verify.sh TASK-H7 >/dev/null 2>&1 && [[ -f state/queue/done/TASK-H7 ]]; then
  ok "verify.sh ignora cruft sin trackear que ya existía antes del claim"
else
  bad "verify.sh no debería rechazar por cruft preexistente al claim"
fi

# Negativo: un archivo NUEVO sin trackear, creado después del claim y fuera
# de scope, sigue detectándose -- confirma que H7 no debilitó el chequeo real.
cat > tasks/TASK-H7B.yaml <<'YAML'
id: TASK-H7B
title: fixture de archivo nuevo sin trackear fuera de scope
objective: probar que un archivo nuevo, no preexistente, sigue detectandose
authority:
  can_modify:
    - artifacts-h7/**
acceptance:
  - n/a
priority: low
status: pending
YAML
./scripts/claim.sh TASK-H7B >/dev/null
echo "archivo nuevo, no preexistente, fuera de scope" > .stray-cruft-after-claim.txt
./scripts/emit_delta.sh TASK-H7B builder "trabajo de prueba" >/dev/null
cat > "state/reviews/TASK-H7B_review.md" <<'MD'
RULE: contracts/rulebook.md#1
ISSUE: none
PASS
MD

if VERIFY_OUT=$(./scripts/verify.sh TASK-H7B 2>&1); then VERIFY_RC=0; else VERIFY_RC=$?; fi
if [[ $VERIFY_RC -ne 0 ]] && echo "$VERIFY_OUT" | grep -q "stray-cruft-after-claim.txt"; then
  ok "un archivo nuevo (no preexistente al claim) sigue detectándose fuera de scope"
else
  bad "un archivo nuevo sin trackear fuera de scope debería seguir rechazándose (rc=$VERIFY_RC)"
fi

echo ""
echo "=== Resultado: $PASS_COUNT passed, $FAIL_COUNT failed ==="
[[ $FAIL_COUNT -eq 0 ]]
