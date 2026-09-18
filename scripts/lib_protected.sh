# scripts/lib_protected.sh — sourced by claim.sh and verify.sh, not run directly.
#
# Defines which paths are protected (contracts/**, decisions/**, AGENTS.md,
# scripts/judge.sh, scripts/verify.sh — see contracts/action-gating.md) and a
# deterministic hash of their combined contents. claim.sh snapshots this hash
# at claim time; verify.sh recomputes it and rejects the task if it changed
# without a recorded human approval (scripts/approve_gate.sh).
#
# Also defines the optional per-task authority.can_modify check (ADR-0006):
# a stricter, opt-in whitelist on top of the global blacklist above. The
# blacklist always applies; the whitelist only applies if a task declares it.

# Este archivo se incluye a sí mismo en la lista. No es redundante: define
# cuáles son las zonas protegidas, así que sin incluirse era el único punto
# desde el cual se podía desactivar el mecanismo entero sin dejar rastro.
# Un tamper *dentro* de una tarea siempre se detectó (el baseline del claim ya
# no coincide), pero uno *persistente*, hecho antes del siguiente claim.sh, no:
# baseline y verificación se habrían calculado con la misma lista ya vaciada y
# habrían coincidido. Hallazgo H1 de TASK-007, expuesto al portar el protocolo
# a un repo ajeno.
PROTECTED_PATHS=(
  "AGENTS.md"
  "contracts"
  "decisions"
  "scripts/judge.sh"
  "scripts/lib_protected.sh"
  "scripts/verify.sh"
)

hash_protected() {
  local files=()
  local p f
  for p in "${PROTECTED_PATHS[@]}"; do
    if [[ -d "$p" ]]; then
      while IFS= read -r f; do files+=("$f"); done < <(find "$p" -type f | sort)
    elif [[ -f "$p" ]]; then
      files+=("$p")
    fi
  done
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "no-protected-files"
    return
  fi
  sha256sum "${files[@]}" | sort | sha256sum | awk '{print $1}'
}

# Cierto si existe un delta agent:human con next_action: approved para esa
# tarea. Único criterio que cuenta como aprobación humana registrada en todo
# el pipeline — usado tanto para zonas protegidas como para el bloque
# authority.can_modify (TASK-008).
has_human_approval() {
  local task_id="$1"
  compgen -G "state/deltas/*_${task_id}_human.yaml" > /dev/null 2>&1 \
    && grep -ql "next_action: approved" state/deltas/*_"${task_id}"_human.yaml
}

# --- authority.can_modify (opt-in, ADR-0006) --------------------------------

# Lista (una por línea) de archivos que cambiaron desde el claim: tracked
# modificados/añadidos respecto al ref grabado, más untracked *nuevos*. Usa
# git — ya es dependencia dura del proyecto, no se reinventa un manifest.
#
# "Nuevos" es la parte que importa: git no versiona qué estaba sin trackear
# en el pasado, así que sin un snapshot propio, cualquier archivo suelto que
# ya existiera *antes* del claim (config local del harness, cache de una
# herramienta, etc.) se contaba para siempre como "cambio de la tarea" —
# bloqueando authority.can_modify por cruft que la tarea nunca tocó.
# claim.sh graba ese snapshot en state/baselines/<task>.untracked_baseline;
# si no existe (tareas reclamadas antes de este fix), se degrada al
# comportamiento anterior en vez de romper retroactivamente.
changed_files_since_claim() {
  local task_id="$1"
  local ref_file="state/baselines/${task_id}.git_ref"
  [[ -f "$ref_file" ]] || return 0
  local ref
  ref="$(cat "$ref_file")"
  local untracked_baseline="state/baselines/${task_id}.untracked_baseline"
  {
    git diff --name-only "$ref" -- . 2>/dev/null
    if [[ -f "$untracked_baseline" ]]; then
      comm -23 <(git ls-files --others --exclude-standard | sort) <(sort "$untracked_baseline")
    else
      git ls-files --others --exclude-standard
    fi
  } | sort -u
}

# --- authority.can_modify integrity (H6, TASK-008) --------------------------
#
# El bloque authority.can_modify de tasks/<id>.yaml no estaba protegido: ni
# el blacklist global (tasks/ no está en PROTECTED_PATHS) ni el propio
# whitelist (tasks/* se exime a sí mismo, ver abajo) lo cubrían. Un builder
# podía ensancharse su propio scope editando tasks/<id>.yaml sin dejar
# rastro detectable. claim.sh graba un hash del bloque al reclamar;
# verify.sh lo recalcula y exige la misma aprobación humana que ya exige
# para zonas protegidas si cambió — mismo patrón, aplicado al task packet.

# Imprime el bloque "authority:" tal cual aparece en el archivo (la línea y
# todo lo indentado debajo), o nada si la tarea no lo declara.
extract_authority_block() {
  local task_file="$1"
  awk '
    /^authority:/ { grab=1; print; next }
    grab && /^[a-zA-Z_]/ { grab=0 }
    grab { print }
  ' "$task_file" 2>/dev/null
}

hash_authority_block() {
  local task_file="$1"
  local block
  block="$(extract_authority_block "$task_file")"
  if [[ -z "$block" ]]; then
    echo "no-authority-declared"
    return
  fi
  echo "$block" | sha256sum | awk '{print $1}'
}

# Imprime (una por línea) los archivos cambiados que quedan fuera de
# authority.can_modify declarado en tasks/<id>.yaml. Vacío si la tarea no
# declara authority (no-op) o si todo cae dentro del scope. Los patrones son
# shell case-patterns: "*" ya matchea "/" (no hace falta "**", aunque
# escribirlo no rompe nada — "**" en case es equivalente a "*").
# state/** y tasks/** siempre están permitidos (bookkeeping del pipeline).
check_task_authority() {
  local task_id="$1" task_file="$2"
  [[ -f "$task_file" ]] || return 0
  grep -q '^authority:' "$task_file" 2>/dev/null || return 0

  local can_modify=() in_can=0 line pat
  while IFS= read -r line; do
    if [[ "$line" =~ ^\ \ can_modify: ]]; then
      in_can=1
      continue
    fi
    if [[ "$line" =~ ^\ \ [a-zA-Z_]+: ]]; then
      in_can=0
    fi
    if [[ $in_can -eq 1 && "$line" =~ ^\ \ \ \ -\ (.+)$ ]]; then
      can_modify+=("${BASH_REMATCH[1]}")
    fi
  done < "$task_file"
  [[ ${#can_modify[@]} -eq 0 ]] && return 0

  local changed
  changed="$(changed_files_since_claim "$task_id")"
  [[ -z "$changed" ]] && return 0

  local f matched
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    case "$f" in
      state/*|tasks/*) continue ;;
    esac
    matched=0
    for pat in "${can_modify[@]}"; do
      case "$f" in
        $pat) matched=1; break ;;
      esac
    done
    [[ $matched -eq 0 ]] && echo "$f"
  done <<< "$changed"
}
